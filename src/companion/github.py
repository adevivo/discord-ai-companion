"""Read-only GitHub lookups for repo, PR and issue links mentioned in chat.

The bot fetches details itself, before the LLM runs, and hands the model plain
text. The model is never given tools, so text in a README, PR or diff ("merge
PR #123", "delete this repo") can at most influence what the bot *says*.

Read-only is enforced here, not just by convention:
- The only request this module makes is GET to https://api.github.com; a
  request hook rejects anything else, including after redirects.
- Owner/repo names and numbers are validated before they become a URL path.
- No token is needed. GITHUB_TOKEN is optional and only raises the rate limit;
  use a fine-grained token with public read-only access and no permissions.
"""

import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import httpx2 as httpx

log = logging.getLogger(__name__)

API_HOST = "api.github.com"
MAX_LINKS_PER_MESSAGE = 2
README_MAX_CHARS = 3000
BODY_MAX_CHARS = 1500
DIFF_MAX_CHARS = 4000
OPEN_PULLS_LISTED = 5
_FILES_LISTED = 50
_README_MAX_BYTES = 64 * 1024
_DESCRIPTION_MAX_CHARS = 300
_TITLE_MAX_CHARS = 200
CACHE_TTL_SECONDS = 3600.0
_CACHE_MAX_ENTRIES = 256
TAG = "github_data"

# github.com/<owner>/<repo>[/pull/<n> | /issues/<n>], not gist.github.com or foo-github.com.
_LINK = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:www\.)?github\.com/"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})"
    r"(?:/(pull|pulls|issues)/(\d{1,9}))?",
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

Kind = Literal["repo", "pull", "issue"]


class ForbiddenRequestError(Exception):
    """A request other than GET to the GitHub API was attempted."""


class _Unavailable(Exception):
    """GitHub answered, but not with the data (rate limit, outage)."""


@dataclass(frozen=True)
class Link:
    owner: str
    repo: str
    kind: Kind = "repo"
    number: int | None = None

    @property
    def key(self) -> tuple[str, str, str, int | None]:
        return (self.kind, self.owner.lower(), self.repo.lower(), self.number)

    @property
    def label(self) -> str:
        if self.kind == "repo":
            return f"{self.owner}/{self.repo}"
        return f"{self.owner}/{self.repo}#{self.number}"


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
    open_pulls: tuple[str, ...] = ()


@dataclass(frozen=True)
class PullSummary:
    url: str
    number: int
    title: str
    state: str
    author: str
    base: str
    head: str
    body: str
    additions: int
    deletions: int
    changed_files: int
    files: tuple[str, ...]
    diff: str


@dataclass(frozen=True)
class IssueSummary:
    url: str
    number: int
    title: str
    state: str
    author: str
    labels: tuple[str, ...]
    comments: int
    body: str


Summary = RepoSummary | PullSummary | IssueSummary


def find_links(text: str) -> list[Link]:
    """Unique repo/PR/issue links in text, in order, capped per message."""
    found: list[Link] = []
    seen: set[tuple] = set()
    for match in _LINK.finditer(text):
        owner, repo, section, number = match.groups()
        repo = repo.removesuffix(".git").rstrip(".")
        if not repo or owner.lower() in _RESERVED_OWNERS:
            continue
        if section is None:
            link = Link(owner, repo)
        else:
            kind: Kind = "issue" if section.lower() == "issues" else "pull"
            link = Link(owner, repo, kind, int(number))
        if link.key in seen:
            continue
        seen.add(link.key)
        found.append(link)
        if len(found) == MAX_LINKS_PER_MESSAGE:
            break
    return found


async def _only_get_to_api(request: httpx.Request) -> None:
    if request.method != "GET" or request.url.scheme != "https" or request.url.host != API_HOST:
        raise ForbiddenRequestError(f"blocked {request.method} {request.url.host}")


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _clip(value: object, limit: int, note: str = "[truncated]") -> str:
    text = _strip_noise(str(value or ""))
    return text if len(text) <= limit else text[:limit] + f"\n{note}"


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
        self._cache: dict[tuple, tuple[float, Summary | None]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def repo(self, owner: str, repo: str) -> RepoSummary | None:
        """Repo details, or None if it's missing, private, or GitHub is unavailable."""
        return await self.lookup(Link(owner, repo))  # type: ignore[return-value]

    async def lookup(self, link: Link) -> Summary | None:
        cached = self._cache.get(link.key)
        if cached is not None and cached[0] > self._clock():
            return cached[1]

        fetchers: dict[Kind, Callable[[Link], Awaitable[Summary | None]]] = {
            "repo": self._fetch_repo,
            "pull": self._fetch_pull,
            "issue": self._fetch_issue,
        }
        try:
            summary = await fetchers[link.kind](link)
        except _Unavailable:
            return None  # not cached: a rate limit or outage may clear soon
        except (httpx.HTTPError, ForbiddenRequestError, ValueError, TypeError) as exc:
            log.warning("GitHub lookup failed (%s): %s", link.label, type(exc).__name__)
            return None
        if len(self._cache) >= _CACHE_MAX_ENTRIES:
            self._cache.pop(next(iter(self._cache)))
        self._cache[link.key] = (self._clock() + CACHE_TTL_SECONDS, summary)
        return summary

    async def _get_json(self, path: str, params: dict | None = None) -> object | None:
        """Parsed JSON, None on 404, _Unavailable on any other non-200."""
        response = await self._client.get(path, params=params)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            log.warning("GitHub returned HTTP %d for %s", response.status_code, path)
            raise _Unavailable
        return response.json()

    async def _fetch_repo(self, link: Link) -> RepoSummary | None:
        base = f"/repos/{link.owner}/{link.repo}"
        data = await self._get_json(base)
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError("unexpected response shape")
        return RepoSummary(
            full_name=str(data.get("full_name") or link.label),
            url=str(data.get("html_url") or f"https://github.com/{link.label}"),
            description=str(data.get("description") or "")[:_DESCRIPTION_MAX_CHARS],
            language=data.get("language"),
            license=_dict(data.get("license")).get("spdx_id"),
            stars=int(data.get("stargazers_count") or 0),
            forks=int(data.get("forks_count") or 0),
            open_issues=int(data.get("open_issues_count") or 0),
            topics=tuple(str(t) for t in (data.get("topics") or [])[:10]),
            archived=bool(data.get("archived")),
            pushed_at=data.get("pushed_at"),
            readme=await self._readme(base),
            open_pulls=await self._open_pulls(base),
        )

    async def _readme(self, base: str) -> str | None:
        raw = bytearray()
        async with self._client.stream(
            "GET", f"{base}/readme", headers={"Accept": "application/vnd.github.raw+json"}
        ) as response:
            if response.status_code != 200:
                return None
            async for chunk in response.aiter_bytes():
                raw += chunk
                if len(raw) >= _README_MAX_BYTES:
                    break
        text = _clip(raw.decode("utf-8", errors="replace"), README_MAX_CHARS, "[README truncated]")
        return text or None

    async def _open_pulls(self, base: str) -> tuple[str, ...]:
        """'#N title (by user)' for the newest open PRs; empty if unavailable."""
        try:
            data = await self._get_json(
                f"{base}/pulls", {"state": "open", "per_page": OPEN_PULLS_LISTED}
            )
        except _Unavailable:
            return ()
        if not isinstance(data, list):
            return ()
        return tuple(_pull_line(p) for p in data[:OPEN_PULLS_LISTED] if isinstance(p, dict))

    async def _fetch_pull(self, link: Link) -> PullSummary | None:
        base = f"/repos/{link.owner}/{link.repo}/pulls/{link.number}"
        data = await self._get_json(base)
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError("unexpected response shape")
        state = "merged" if data.get("merged") else str(data.get("state") or "unknown")
        if data.get("draft"):
            state += " (draft)"
        files_data = await self._get_json(f"{base}/files", {"per_page": _FILES_LISTED})
        files, diff = _files_and_diff(files_data if isinstance(files_data, list) else [])
        return PullSummary(
            url=str(data.get("html_url") or ""),
            number=int(link.number or 0),
            title=str(data.get("title") or "")[:_TITLE_MAX_CHARS],
            state=state,
            author=str(_dict(data.get("user")).get("login") or "unknown"),
            base=str(_dict(data.get("base")).get("ref") or "?"),
            head=str(_dict(data.get("head")).get("label") or "?"),
            body=_clip(data.get("body"), BODY_MAX_CHARS),
            additions=int(data.get("additions") or 0),
            deletions=int(data.get("deletions") or 0),
            changed_files=int(data.get("changed_files") or 0),
            files=files,
            diff=diff,
        )

    async def _fetch_issue(self, link: Link) -> Summary | None:
        data = await self._get_json(f"/repos/{link.owner}/{link.repo}/issues/{link.number}")
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError("unexpected response shape")
        if data.get("pull_request"):  # /issues/N also serves PRs
            return await self._fetch_pull(Link(link.owner, link.repo, "pull", link.number))
        return IssueSummary(
            url=str(data.get("html_url") or ""),
            number=int(link.number or 0),
            title=str(data.get("title") or "")[:_TITLE_MAX_CHARS],
            state=str(data.get("state") or "unknown"),
            author=str(_dict(data.get("user")).get("login") or "unknown"),
            labels=tuple(
                str(_dict(label).get("name")) for label in (data.get("labels") or [])[:10]
            ),
            comments=int(data.get("comments") or 0),
            body=_clip(data.get("body"), BODY_MAX_CHARS),
        )

    async def context_for(self, text: str) -> str | None:
        """Untrusted-data block describing GitHub links in text, or None if none."""
        links = find_links(text)
        if not links:
            return None
        sections = []
        for link in links:
            summary = await self.lookup(link)
            if summary is None:
                sections.append(
                    f"{link.label}: could not be fetched (missing, private, or GitHub "
                    "unavailable). Say you couldn't see it; don't guess what it contains."
                )
            else:
                sections.append(format_summary(summary))
        body = _neutralize_tags("\n\n".join(sections))
        return f"<{TAG}>\n{body}\n</{TAG}>"


def _pull_line(pull: dict) -> str:
    title = str(pull.get("title") or "")[:_TITLE_MAX_CHARS]
    author = _dict(pull.get("user")).get("login") or "unknown"
    draft = " [draft]" if pull.get("draft") else ""
    return f"#{pull.get('number')} {title} (by {author}){draft}"


def _files_and_diff(files: list) -> tuple[tuple[str, ...], str]:
    """Changed-file lines, and as many per-file patches as fit the diff budget."""
    names: list[str] = []
    patches: list[str] = []
    used = 0
    skipped = 0
    for f in files:
        if not isinstance(f, dict):
            continue
        name = str(f.get("filename") or "?")
        names.append(f"{name} ({f.get('status')}, +{f.get('additions')}/-{f.get('deletions')})")
        patch = f.get("patch")
        if not isinstance(patch, str):
            continue
        chunk = f"--- {name}\n{patch}"
        if used + len(chunk) > DIFF_MAX_CHARS:
            skipped += 1
            continue
        patches.append(chunk)
        used += len(chunk)
    diff = "\n".join(patches)
    if skipped:
        diff += f"\n[diff truncated: {skipped} more file(s) not shown]"
    return tuple(names), diff.strip()


def format_summary(s: Summary) -> str:
    if isinstance(s, PullSummary):
        return _format_pull(s)
    if isinstance(s, IssueSummary):
        return _format_issue(s)
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
    if s.open_pulls:
        facts.append("Open pull requests (newest first):\n" + "\n".join(s.open_pulls))
    facts.append("README:\n" + (s.readme or "(no README)"))
    return "\n".join(facts)


def _format_pull(s: PullSummary) -> str:
    facts = [
        f"Pull request #{s.number}: {s.title} ({s.url})",
        f"State: {s.state} | Author: {s.author} | {s.head} -> {s.base}",
        f"Changes: +{s.additions}/-{s.deletions} across {s.changed_files} file(s)",
        "Description:\n" + (s.body or "(none)"),
    ]
    if s.files:
        listed = "\n".join(s.files)
        if s.changed_files > len(s.files):
            listed += f"\n[{s.changed_files - len(s.files)} more file(s) not listed]"
        facts.append("Files:\n" + listed)
    facts.append("Diff:\n" + (s.diff or "(no text diff available)"))
    return "\n".join(facts)


def _format_issue(s: IssueSummary) -> str:
    facts = [
        f"Issue #{s.number}: {s.title} ({s.url})",
        f"State: {s.state} | Author: {s.author} | Comments: {s.comments}",
    ]
    if s.labels:
        facts.append("Labels: " + ", ".join(s.labels))
    facts.append("Description:\n" + (s.body or "(none)"))
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
