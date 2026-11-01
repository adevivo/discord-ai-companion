"""Read-only GitHub lookups for repo links mentioned in chat.

The bot fetches repo details itself, before the LLM runs, and hands the model
plain text. The model is never given tools, so text in a README ("merge PR
#123", "delete this repo") can at most influence what the bot *says*.

Read-only is enforced here, not just by convention:
- The only request this module makes is GET to https://api.github.com; a
  request hook rejects anything else, including after redirects.
- Owner/repo names are validated before they become a URL path.
- No token is needed. GITHUB_TOKEN is optional and only raises the rate limit;
  use a fine-grained token with public read-only access and no permissions.
"""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx2 as httpx

log = logging.getLogger(__name__)

API_HOST = "api.github.com"
MAX_REPOS_PER_MESSAGE = 2
README_MAX_CHARS = 3000
_README_MAX_BYTES = 64 * 1024
_DESCRIPTION_MAX_CHARS = 300
CACHE_TTL_SECONDS = 3600.0
_CACHE_MAX_ENTRIES = 256
TAG = "github_data"

# github.com/<owner>/<repo>, not gist.github.com or foo-github.com.
_REPO_LINK = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:www\.)?github\.com/"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})",
    re.IGNORECASE,
)
# First path segments that are GitHub pages, not users or orgs.
_RESERVED_OWNERS = frozenset(
    {
        "about", "apps", "collections", "enterprise", "explore", "features", "login",
        "marketplace", "notifications", "orgs", "pricing", "search", "settings",
        "sponsors", "topics", "users",
    }
)  # fmt: skip


class ForbiddenRequestError(Exception):
    """A request other than GET to the GitHub API was attempted."""


@dataclass(frozen=True)
class RepoSummary:
    full_name: str
    url: str
    description: str
    language: str | None
    license: str | None
    stars: int
    forks: int
    open_issues: int
    topics: tuple[str, ...]
    archived: bool
    pushed_at: str | None
    readme: str | None


def find_repos(text: str) -> list[tuple[str, str]]:
    """Unique (owner, repo) pairs linked in text, in order, capped per message."""
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in _REPO_LINK.finditer(text):
        owner, repo = match.group(1), match.group(2)
        repo = repo.removesuffix(".git").rstrip(".")
        if not repo or owner.lower() in _RESERVED_OWNERS:
            continue
        key = (owner.lower(), repo.lower())
        if key in seen:
            continue
        seen.add(key)
        found.append((owner, repo))
        if len(found) == MAX_REPOS_PER_MESSAGE:
            break
    return found


async def _only_get_to_api(request: httpx.Request) -> None:
    if request.method != "GET" or request.url.scheme != "https" or request.url.host != API_HOST:
        raise ForbiddenRequestError(f"blocked {request.method} {request.url.host}")


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=f"https://{API_HOST}",
            headers=headers,
            timeout=10.0,
            follow_redirects=True,  # renamed repos redirect; the hook re-checks each hop
            event_hooks={"request": [_only_get_to_api]},
            transport=transport,
        )
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, RepoSummary | None]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def repo(self, owner: str, repo: str) -> RepoSummary | None:
        """Repo details, or None if it's missing, private, or GitHub is unavailable."""
        key = (owner.lower(), repo.lower())
        cached = self._cache.get(key)
        if cached is not None and cached[0] > self._clock():
            return cached[1]

        try:
            summary, cacheable = await self._fetch(owner, repo)
        except (httpx.HTTPError, ForbiddenRequestError, ValueError) as exc:
            log.warning("GitHub lookup failed (%s/%s): %s", owner, repo, type(exc).__name__)
            return None
        if cacheable:
            if len(self._cache) >= _CACHE_MAX_ENTRIES:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (self._clock() + CACHE_TTL_SECONDS, summary)
        return summary

    async def _fetch(self, owner: str, repo: str) -> tuple[RepoSummary | None, bool]:
        """(summary, cacheable). Not-found is cached; rate limits and outages aren't."""
        response = await self._client.get(f"/repos/{owner}/{repo}")
        if response.status_code == 404:
            return None, True
        if response.status_code != 200:
            log.warning("GitHub returned HTTP %d for %s/%s", response.status_code, owner, repo)
            return None, False
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("unexpected response shape")
        license_info = data.get("license")
        license_info = license_info if isinstance(license_info, dict) else {}
        summary = RepoSummary(
            full_name=str(data.get("full_name") or f"{owner}/{repo}"),
            url=str(data.get("html_url") or f"https://github.com/{owner}/{repo}"),
            description=str(data.get("description") or "")[:_DESCRIPTION_MAX_CHARS],
            language=data.get("language"),
            license=license_info.get("spdx_id"),
            stars=int(data.get("stargazers_count") or 0),
            forks=int(data.get("forks_count") or 0),
            open_issues=int(data.get("open_issues_count") or 0),
            topics=tuple(str(t) for t in (data.get("topics") or [])[:10]),
            archived=bool(data.get("archived")),
            pushed_at=data.get("pushed_at"),
            readme=await self._readme(owner, repo),
        )
        return summary, True

    async def _readme(self, owner: str, repo: str) -> str | None:
        raw = bytearray()
        async with self._client.stream(
            "GET",
            f"/repos/{owner}/{repo}/readme",
            headers={"Accept": "application/vnd.github.raw+json"},
        ) as response:
            if response.status_code != 200:
                return None
            async for chunk in response.aiter_bytes():
                raw += chunk
                if len(raw) >= _README_MAX_BYTES:
                    break
        text = _strip_noise(raw.decode("utf-8", errors="replace"))
        if len(text) > README_MAX_CHARS:
            text = text[:README_MAX_CHARS] + "\n[README truncated]"
        return text or None

    async def context_for(self, text: str) -> str | None:
        """Untrusted-data block describing repos linked in text, or None if none."""
        repos = find_repos(text)
        if not repos:
            return None
        sections = []
        for owner, name in repos:
            summary = await self.repo(owner, name)
            if summary is None:
                sections.append(
                    f"{owner}/{name}: could not be fetched (missing, private, or GitHub "
                    "unavailable). Say you couldn't see it; don't guess what it contains."
                )
            else:
                sections.append(format_summary(summary))
        body = _neutralize_tags("\n\n".join(sections))
        return f"<{TAG}>\n{body}\n</{TAG}>"


def format_summary(s: RepoSummary) -> str:
    facts = [
        f"Repository: {s.full_name} ({s.url})",
        f"Description: {s.description or '(none)'}",
        f"Language: {s.language or 'unknown'} | License: {s.license or 'none'}",
        f"Stars: {s.stars} | Forks: {s.forks} | Open issues + PRs: {s.open_issues}",
    ]
    if s.topics:
        facts.append("Topics: " + ", ".join(s.topics))
    if s.pushed_at:
        facts.append(f"Last push: {s.pushed_at}")
    if s.archived:
        facts.append("Status: archived (read-only)")
    facts.append("README:\n" + (s.readme or "(no README)"))
    return "\n".join(facts)


_HTML_TAG = re.compile(r"<[^>\n]{1,500}>")
_MD_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_LINES = re.compile(r"\n{3,}")


def _strip_noise(text: str) -> str:
    """Drop HTML tags and images (badges, logos): tokens the model can't use."""
    text = _TRAILING_SPACE.sub("", _MD_IMAGE.sub("", _HTML_TAG.sub("", text)))
    return _BLANK_LINES.sub("\n\n", text).strip()


_TAG_PATTERN = re.compile(rf"<\s*/?\s*{TAG}", re.IGNORECASE)


def _neutralize_tags(text: str) -> str:
    """Stop fetched text from closing (or reopening) the data block early."""
    return _TAG_PATTERN.sub(lambda m: m.group(0).replace("<", "‹"), text)
