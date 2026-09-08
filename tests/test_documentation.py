"""Public documentation entry points and repository-relative links."""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urlsplit

_ROOT = Path(__file__).parents[1]


def test_documentation_languages_have_matching_pages() -> None:
    english = {
        path.relative_to(_ROOT / "docs/en")
        for path in (_ROOT / "docs/en").rglob("*.md")
    }
    chinese = {
        path.relative_to(_ROOT / "docs/cn")
        for path in (_ROOT / "docs/cn").rglob("*.md")
    }
    assert english == chinese
    assert {
        Path("index.md"),
        Path("quick_start.md"),
        Path("studio/quick_start.md"),
        Path("runtime/quick_start.md"),
    } <= english
    for language, filename in [("en", "README.md"), ("cn", "README.cn.md")]:
        content = (_ROOT / filename).read_text()
        section = content.split("## Documentation\n", 1)[1].split("\n## ", 1)[0]
        assert f"docs/{language}/index.md" in section
        assert "README.md" in content and "README.cn.md" in content
        assert all(
            f"docs/assets/screenshots/{scene}-{language}.png" in content
            for scene in ["studio", "plan", "trace"]
        )


def test_public_documentation_links_resolve_inside_the_repository() -> None:
    documents = [*_ROOT.glob("README*.md"), *(_ROOT / "docs").rglob("*.md")]
    documents += list((_ROOT / "apps").glob("studio/*/README.md"))
    documents += list((_ROOT / "packages").glob("*/README.md"))
    missing: list[str] = []
    for document in documents:
        content = re.sub(r"```.*?```", "", document.read_text(), flags=re.DOTALL)
        links = re.findall(r"\]\(([^\s)]+)\)", content)
        links += re.findall(r'(?:href|src)="([^"]+)"', content)
        for link in links:
            parsed = urlsplit(unescape(link))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = document.parent / unquote(parsed.path)
            if not target.exists():
                missing.append(f"{document.relative_to(_ROOT)}: {link}")
    assert missing == []
