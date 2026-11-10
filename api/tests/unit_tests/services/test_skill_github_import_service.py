"""Public GitHub imports stay bounded and select only the requested Skill."""

import io
import stat
import zipfile
from unittest.mock import patch

import httpx
import pytest

from configs import dify_config
from services.skill_github_import_service import SkillGithubImportService
from services.skill_management_service import SkillManagementServiceError

SHA = "a" * 40
SKILL_MD = b"---\nname: example\ndescription: Example skill\n---\nInstructions\n"


def archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, content in files.items():
            target.writestr(name, content)
    return output.getvalue()


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/org/repo",
        "https://github.com.evil.test/org/repo",
        "https://user@github.com/org/repo",
        "https://github.com:443/org/repo",
        "https://github.com/org/repo/blob/main/SKILL.md",
        "https://github.com/org/repo/tree",
        "https://github.com/org/repo?token=secret",
        "https://github.com/org/repo/tree/main/../x",
        "https://github.com/org/repo/tree/main/%2e%2e/x",
        "https://github.com/org/repo/tree/main/a%5Cb",
    ],
)
def test_rejects_non_repository_urls_before_network(url):
    with patch.object(SkillGithubImportService, "_fetch") as fetch:
        with pytest.raises(SkillManagementServiceError) as exc:
            SkillGithubImportService.download(url)
        assert exc.value.code == "invalid_github_url"
        fetch.assert_not_called()


@pytest.mark.parametrize("directory", ["", "skills/example"])
def test_download_resolves_commit_and_selects_only_skill(directory):
    prefix = "repo-" + SHA + "/" + (directory + "/" if directory else "")
    data = archive(
        {
            prefix + "SKILL.md": SKILL_MD,
            prefix + "assets/icon.png": b"\x89PNG",
            "repo-" + SHA + "/elsewhere/secret": b"excluded",
        }
    )
    responses = [httpx.Response(200, json={"private": False, "default_branch": "main"})]
    if directory:
        responses.extend([httpx.Response(422), httpx.Response(404)])
    responses.extend([httpx.Response(200, json={"sha": SHA}), httpx.Response(200, content=data)])
    with patch.object(SkillGithubImportService, "_fetch", side_effect=responses) as fetch:
        payload = SkillGithubImportService.download(
            "https://github.com/org/repo" + ("/tree/main/" + directory if directory else "")
        )
    assert fetch.call_args.args[0] == f"https://codeload.github.com/org/repo/zip/{SHA}"
    assert fetch.call_args.kwargs["archive"] is True
    with zipfile.ZipFile(io.BytesIO(payload.content)) as result:
        assert result.read("SKILL.md") == SKILL_MD
        if directory:
            assert set(result.namelist()) == {"SKILL.md", "assets/icon.png"}


def test_slash_ref_is_resolved_before_directory():
    responses = [
        httpx.Response(200, json={"private": False}),
        httpx.Response(404),
        httpx.Response(200, json={"sha": SHA}),
        httpx.Response(200, content=archive({"root/skill/SKILL.md": SKILL_MD})),
    ]
    with patch.object(SkillGithubImportService, "_fetch", side_effect=responses) as fetch:
        SkillGithubImportService.download("https://github.com/org/repo/tree/feature/new/skill")
    assert fetch.call_args_list[2].args[0].endswith("/commits/feature%2Fnew?per_page=1")


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(200, json={"private": True}), "github_not_found"),
        (httpx.Response(404), "github_not_found"),
        (httpx.Response(422), "github_not_found"),
        (httpx.Response(403), "github_rate_limited"),
        (httpx.Response(429), "github_rate_limited"),
        (httpx.Response(302, headers={"location": "http://localhost"}), "github_download_failed"),
        (httpx.Response(200, content=b"not json"), "github_download_failed"),
    ],
)
def test_unavailable_repository_is_not_imported(response, code):
    with patch.object(SkillGithubImportService, "_fetch", return_value=response):
        with pytest.raises(SkillManagementServiceError) as exc:
            SkillGithubImportService.download("https://github.com/org/repo")
    assert exc.value.code == code


@pytest.mark.parametrize("path", ["../outside", "/absolute", "scripts/../../outside", "a\\b"])
def test_rejects_unsafe_selected_paths(path):
    data = archive({"root/skill/SKILL.md": SKILL_MD, "root/skill/" + path: b"unsafe"})
    with pytest.raises(SkillManagementServiceError) as exc:
        SkillGithubImportService._select_directory(data, "skill")
    assert exc.value.code == "invalid_skill_package"


def test_rejects_symlinks():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        target.writestr("root/SKILL.md", SKILL_MD)
        info = zipfile.ZipInfo("root/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        target.writestr(info, "../../secret")
    with pytest.raises(SkillManagementServiceError, match="Unsafe"):
        SkillGithubImportService._select_directory(output.getvalue(), "")


def test_missing_skill_md_does_not_search_nested_skills():
    with pytest.raises(SkillManagementServiceError) as exc:
        SkillGithubImportService._select_directory(archive({"root/skills/example/SKILL.md": SKILL_MD}), "")
    assert exc.value.code == "missing_skill_md"


def test_rejects_decompression_limit_before_reading(monkeypatch):
    monkeypatch.setattr(dify_config, "SKILL_PACKAGE_MAX_UNCOMPRESSED_BYTES", 1)
    with patch.object(zipfile.ZipFile, "read") as read:
        with pytest.raises(SkillManagementServiceError) as exc:
            SkillGithubImportService._select_directory(archive({"root/SKILL.md": SKILL_MD}), "")
    assert exc.value.code == "skill_too_large"
    read.assert_not_called()


def test_stream_download_is_bounded_and_closed():
    response = httpx.Response(200, stream=httpx.ByteStream(b"too large"))
    with patch("services.skill_github_import_service.remote_fetcher.make_request", return_value=response) as get:
        with pytest.raises(SkillManagementServiceError) as exc:
            SkillGithubImportService._fetch("https://codeload.github.com/org/repo/zip/sha", limit=1, archive=True)
    assert exc.value.code == "github_download_too_large"
    assert response.is_closed
    assert get.call_args.kwargs["follow_redirects"] is False
    assert get.call_args.kwargs["stream_response"] is True


def test_invalid_archive_is_rejected():
    with pytest.raises(SkillManagementServiceError) as exc:
        SkillGithubImportService._select_directory(b"not a zip", "")
    assert exc.value.code == "invalid_skill_package"


def test_repository_entry_count_is_bounded(monkeypatch):
    monkeypatch.setattr(dify_config, "SKILL_PACKAGE_MAX_ENTRIES", 1)
    data = archive({"root/SKILL.md": SKILL_MD, "root/other": b"other"})
    with pytest.raises(SkillManagementServiceError) as exc:
        SkillGithubImportService._select_directory(data, "")
    assert exc.value.code == "too_many_files"


def test_duplicate_paths_are_rejected():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        target.writestr("root/SKILL.md", SKILL_MD)
        with pytest.warns(UserWarning, match="Duplicate"):
            target.writestr("root/SKILL.md", SKILL_MD)
    with pytest.raises(SkillManagementServiceError) as exc:
        SkillGithubImportService._select_directory(output.getvalue(), "")
    assert exc.value.code == "invalid_skill_package"
