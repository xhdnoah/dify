"""Fetch a public GitHub Skill snapshot before opening a database transaction.

Only GitHub repository and tree URLs are accepted. Resolve refs to immutable
commits, download without credentials or redirects, and repackage the selected
folder for the existing Skill importer. No repository code is executed.
"""

from __future__ import annotations

import io
import re
import stat
import zipfile
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from configs import dify_config
from core.file import remote_fetcher
from core.helper import ssrf_proxy
from core.tools.errors import ToolSSRFError
from services.skill_management_service import SkillImportPayload, SkillManagementService, SkillManagementServiceError


def _error(code: str, message: str) -> SkillManagementServiceError:
    return SkillManagementServiceError(code, message)


class SkillGithubImportService:
    @staticmethod
    def _parse_url(url: str) -> tuple[str, str, list[str]]:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or parsed.query or parsed.fragment:
            raise _error("invalid_github_url", "Use an HTTPS github.com repository or directory URL.")
        parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
        if len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts[:2]):
            raise _error("invalid_github_url", "GitHub owner and repository are required.")
        if any(part in {"", ".", ".."} or "\\" in part or "\x00" in part for part in parts):
            raise _error("invalid_github_url", "Invalid GitHub URL path.")
        owner, repository = parts[:2]
        repository = repository.removesuffix(".git")
        if (
            repository in {"", ".", ".."}
            or (len(parts) > 2 and (parts[2] != "tree" or len(parts) < 4))
            or len(parts) > 20
        ):
            raise _error("invalid_github_url", "Use a repository URL or a /tree/ref/directory URL.")
        return owner, repository, parts[3:]

    @staticmethod
    def _fetch(url: str, *, limit: int, archive: bool = False) -> httpx.Response:
        try:
            fetch = remote_fetcher.make_request if archive else ssrf_proxy.make_request
            response = fetch(
                "GET",
                url,
                max_retries=0,
                stream_response=True,
                follow_redirects=False,
                timeout=30,
                headers={"Accept-Encoding": "identity", "User-Agent": "Dify-Skill-Import"},
            )
            return ssrf_proxy.buffer_response(response, max_response_bytes=limit)
        except ssrf_proxy.ResponseLimitError as exc:
            raise _error("github_download_too_large", "GitHub response exceeds the import size limit.") from exc
        except (httpx.RequestError, ToolSSRFError) as exc:
            raise _error("github_download_failed", "Could not download the public GitHub repository.") from exc

    @classmethod
    def _metadata(cls, url: str) -> dict[str, Any]:
        response = cls._fetch(url, limit=1024 * 1024)
        if response.status_code in {404, 422}:
            raise _error("github_not_found", "Public repository or ref not found.")
        if response.status_code in {403, 429}:
            raise _error("github_rate_limited", "GitHub refused the request or its API rate limit was reached.")
        if response.status_code != 200:
            raise _error("github_download_failed", "GitHub could not provide the repository.")
        try:
            data = response.json()
        except ValueError as exc:
            raise _error("github_download_failed", "Invalid GitHub response.") from exc
        if not isinstance(data, dict):
            raise _error("github_download_failed", "Invalid GitHub response.")
        return data

    @classmethod
    def download(cls, url: str) -> SkillImportPayload:
        owner, repository, tail = cls._parse_url(url.strip())
        base = f"https://api.github.com/repos/{owner}/{repository}"
        metadata = cls._metadata(base)
        if metadata.get("private") is not False:
            raise _error("github_not_found", "Only public GitHub repositories are supported.")
        subdirectory = ""
        if tail:
            # GitHub tree URLs are ambiguous when refs contain '/'. Prefer the
            # longest existing ref, and treat the remainder as the directory.
            for length in range(len(tail), 0, -1):
                try:
                    commit = cls._metadata(f"{base}/commits/{quote('/'.join(tail[:length]), safe='')}?per_page=1")
                except SkillManagementServiceError as exc:
                    if exc.code == "github_not_found":
                        continue
                    raise
                subdirectory = "/".join(tail[length:])
                break
            else:
                raise _error("github_not_found", "GitHub ref not found.")
        else:
            branch = metadata.get("default_branch")
            if not isinstance(branch, str) or not branch:
                raise _error("github_not_found", "Repository has no default branch.")
            commit = cls._metadata(f"{base}/commits/{quote(branch, safe='')}?per_page=1")
        sha = commit.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise _error("github_download_failed", "Invalid GitHub commit response.")
        limit = dify_config.UPLOAD_SKILL_FILE_SIZE_LIMIT * 1024 * 1024
        response = cls._fetch(
            f"https://codeload.github.com/{owner}/{repository}/zip/{sha}",
            limit=limit,
            archive=True,
        )
        if response.status_code != 200:
            raise _error("github_download_failed", "Could not download the repository archive.")
        return SkillImportPayload(
            content=cls._select_directory(response.content, subdirectory), filename="github-skill.zip"
        )

    @staticmethod
    def _select_directory(content: bytes, directory: str) -> bytes:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as source:
                infos = source.infolist()
                if not infos or len(infos) > dify_config.SKILL_PACKAGE_MAX_ENTRIES:
                    raise _error("too_many_files", "Repository archive exceeds the file count limit.")
                roots = {info.filename.split("/", 1)[0] for info in infos}
                if len(roots) != 1:
                    raise _error("invalid_skill_package", "Invalid GitHub archive root.")
                root = next(iter(roots))
                if root in {"", ".", ".."} or "\\" in root:
                    raise _error("invalid_skill_package", "Invalid GitHub archive root.")
                prefix = root + "/" + (directory + "/" if directory else "")
                selected = [info for info in infos if info.filename.startswith(prefix) and not info.is_dir()]
                paths: set[str] = set()
                for info in selected:
                    path = info.filename[len(prefix) :]
                    if (
                        any(part in {"", ".", ".."} for part in path.split("/"))
                        or "\\" in path
                        or "\x00" in path
                        or path in paths
                        or stat.S_ISLNK(info.external_attr >> 16)
                    ):
                        raise _error("invalid_skill_package", "Unsafe or duplicate repository file path.")
                    paths.add(path)
                if "SKILL.md" not in paths:
                    raise _error(
                        "missing_skill_md", "Selected directory must contain SKILL.md. Use a Skill directory URL."
                    )
                # Bound decompression before reading any repository member.
                SkillManagementService._validate_archive_limits(source)
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
                    for info in selected:
                        target.writestr(info.filename[len(prefix) :], source.read(info))
                result = output.getvalue()
                if len(result) > dify_config.UPLOAD_SKILL_FILE_SIZE_LIMIT * 1024 * 1024:
                    raise _error("archive_too_large", "Skill archive exceeds size limit.")
                return result
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise _error("invalid_skill_package", "Invalid GitHub archive.") from exc
