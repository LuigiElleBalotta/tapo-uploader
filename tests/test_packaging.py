"""Packaging and documentation of the storage destinations."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_dockerfile_copies_storage_package_and_installs_rclone():
    dockerfile = read("Dockerfile")
    assert "COPY tapo_storage ./tapo_storage" in dockerfile
    assert re.search(r"apt-get install[^\n]*\\?\n?[^\n]*\brclone\b", dockerfile)


def test_requirements_txt_still_three_packages():
    lines = [l for l in read("requirements.txt").splitlines() if l.strip() and not l.startswith("#")]
    assert len(lines) == 3


def test_env_example_documents_every_storage_variable():
    source = read("tapo_storage/base.py")
    names = set(re.findall(r'(?:need|environ\.get)\("([A-Z_]+)"', source)) | {"STORAGE"}
    assert {"WEBDAV_URL", "LOCAL_ROOT", "RCLONE_REMOTE"} <= names
    example = read(".env.example")
    assert not sorted(n for n in names if n not in example)


def test_readmes_document_storage_destinations():
    for name, honest in (("README.md", "not tested"), ("README_IT.md", "non l'ha provato")):
        text = read(name)
        for needed in ("STORAGE", "WEBDAV_URL", "LOCAL_ROOT", "RCLONE_REMOTE", "ALLOW_OWN_STORAGE", "STORAGE_ALLOWED",
                       "Mega", "H.264", "Watch here", "Open the video"):
            assert needed in text, f"{name} lacks {needed}"
        assert honest in text, f"{name} must say Mega was not tested"


def test_index_mentions_storage_choices_and_stays_asset_free():
    html = read("index.html")
    for needed in ('id="storage"', "WebDAV", "rclone", "Mega", "Watch here", "Open the video", 'href="#storage"'):
        assert needed in html, needed
    refs = re.findall(r'<(?:link|script|img)\b[^>]*?(?:href|src)="([^"]+)"', html)
    own = "https://luigiellebalotta.github.io/tapo-uploader/"
    others = [r for r in refs if re.match(r"^(https?:)?//", r) and not r.startswith(own)]
    assert refs and not others, "the landing page must load nothing from other sites"
    assert "keeps nobody's footage" not in html  # no longer true once a server can host the videos


def test_index_sections_still_alternate_backgrounds():
    html = read("index.html")
    kinds = re.findall(r'<section class="section( section-alt)?" id="(how|features|friends|storage|run|start|faq)"', html)
    order = [(ident, bool(alt)) for alt, ident in kinds]
    assert [i for i, _ in order] == ["how", "features", "friends", "storage", "run", "start", "faq"]
    flags = [alt for _, alt in order]
    assert all(a != b for a, b in zip(flags, flags[1:])), order


def test_privacy_policy_covers_what_google_and_users_need():
    html = read("privacy.html")
    for needed in ("Limited Use", "youtube", "myaccount.google.com/permissions", "30 days", "7 days", "encrypted",
                   "If you run it yourself", "api-services-user-data-policy", "Last updated"):
        assert needed in html, needed
    assert "used only by its author" not in html and "not distributed to or used by other people" not in html
    refs = re.findall(r'<(?:link|script|img)\b[^>]*?(?:href|src)="([^"]+)"', html)
    own = "https://luigiellebalotta.github.io/tapo-uploader/"
    assert not [r for r in refs if re.match(r"^(https?:)?//", r) and not r.startswith(own)]


def test_readmes_tell_people_to_use_their_own_google_pages():
    assert "Use your own pages, not this project's" in read("README.md")
    assert "Usa le tue pagine, non quelle di questo progetto" in read("README_IT.md")


def test_workflow_tests_then_publishes_a_multi_arch_image_to_ghcr():
    flow = read(".github/workflows/docker.yml")
    for needed in ("ghcr.io/luigiellebalotta/tapo-uploader", "packages: write", "linux/amd64,linux/arm64",
                   "python -m pytest tests", "needs: test", "type=raw,value=latest", "type=semver"):
        assert needed in flow, needed
    assert "github.event_name != 'pull_request'" in flow  # pull requests never publish
    assert flow.count("packages: write") == 1 and "pull_request_target" not in flow


def test_compose_file_runs_the_web_app_from_the_published_image():
    compose = read("deploy/docker-compose.yml")
    assert "ghcr.io/luigiellebalotta/tapo-uploader" in compose and "tapo_web" in compose
    assert "127.0.0.1:8080:8080" in compose and "no-new-privileges" in compose


def test_readmes_document_the_docker_image():
    for name in ("README.md", "README_IT.md"):
        text = read(name)
        assert "ghcr.io/luigiellebalotta/tapo-uploader" in text and "docker-compose.yml" in text, name


def test_storage_guide_page_covers_every_destination_and_is_linked():
    guide = read("storage-guide.html")
    for needed in ('id="youtube"', 'id="onedrive"', 'id="webdav"', 'id="rclone"', 'id="folder"', 'id="switching"',
                   "MS_CLIENT_ID", "MS_CLIENT_SECRET", "Files.ReadWrite.AppFolder", "/storage/onedrive/callback",
                   "/youtube/callback", "app password", "rclone config", "STORAGE_LOCAL_ROOT", "ALLOW_OWN_STORAGE"):
        assert needed in guide, needed
    refs = re.findall(r'<(?:link|script|img)\b[^>]*?(?:href|src)="([^"]+)"', guide)
    own = "https://luigiellebalotta.github.io/tapo-uploader/"
    others = [r for r in refs if re.match(r"^(https?:)?//", r) and not r.startswith(own)]
    assert refs and not others, "the guide must load nothing from other sites"
    assert 'href="storage-guide.html"' in read("index.html") and "storage-guide.html" in read("sitemap.xml")
    assert "storage-guide.html" in read("README.md") and "storage-guide.html" in read("README_IT.md")


def test_guide_names_match_the_settings_the_code_reads():
    guide, settings = read("storage-guide.html"), read("tapo_web/config.py")
    for name in ("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT", "STORAGE_RCLONE_REMOTE", "STORAGE_LOCAL_ROOT",
                 "STORAGE_WEBDAV_URL", "YOUTUBE_CLIENT_SECRET_JSON", "ALLOW_OWN_STORAGE"):
        assert name in guide and name in settings, name
