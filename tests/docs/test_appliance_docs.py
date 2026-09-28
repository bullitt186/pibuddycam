"""Documentation contract tests.

Stdlib-only; no network or hardware. They keep the documentation navigable and
safe to publish:

* the community and user/contributor pages exist where the README links them;
* agent instructions live in one small AGENTS.md that CLAUDE.md imports;
* every relative markdown link and ``#anchor`` in maintained docs resolves
  (``_archive/`` is historical and exempt as a *source*, not as a target);
* no personal literals or the formerly leaked device MAC appear in any tracked
  text file;
* the user docs never overstate hardware acceptance.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_FILES = (
    "README.md", "LICENSE", "NOTICE.md", "CONTRIBUTING.md", "SECURITY.md",
    "CODE_OF_CONDUCT.md", "CHANGELOG.md", "AGENTS.md", "CLAUDE.md",
    "docs/README.md", "docs/install.md", "docs/user-guide.md", "docs/integrations.md",
    "docs/troubleshooting.md", "docs/hardware.md", "docs/status.md", "docs/roadmap.md",
    "docs/architecture.md", "docs/development.md", "docs/releasing.md",
    "docs/reverse-engineering/README.md", "docs/reverse-engineering/protocol.md",
    "docs/reverse-engineering/gap-tracker.md", "docs/agents/README.md",
    "_archive/README.md", "app/README.md", "image/README.md",
)

#: Personal literals the secret scanner flags. Assembled from fragments (like
#: scan-secrets.sh) so this file does not itself contain the literal.
PERSONAL_LITERALS = ("b" "u" "l" "l" "i" "t" "t", "s" "t" "a" "h" "m" "e" "r")
#: The real device MAC and fingerprint once committed by mistake (fragments).
LEAKED_IDENTITY = ("d8:3a:" "dd:32", "142486" "ddfee8")
#: The public GitHub handle (the owner in URLs, badges and the LICENSE) is the
#: one allowed form; a bare local username or home path is not.
PUBLIC_HANDLE = re.compile("b" "u" "l" "l" "i" "t" "t" r"186\b")

#: Files that legitimately contain the scanner's own patterns or synthetic data.
PERSONAL_SCAN_EXEMPT = ("image/scripts/scan-secrets.sh", "tests/")

FORBIDDEN_ACCEPTANCE_CLAIMS = (
    "hardware acceptance is complete",
    "all acceptance sections passed",
    "release matrix is complete",
)

AGENTS_MAX_LINES = 80


def _tracked(pattern=None):
    args = ["git", "ls-files"] + ([pattern] if pattern else [])
    out = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return [p for p in out.stdout.splitlines() if (REPO_ROOT / p).is_file()]


def _strip_code(text):
    return re.sub(r"```.*?```", "", text, flags=re.S)


def _anchors(path):
    """GitHub-style heading slugs plus explicit ``<a id>`` anchors."""
    text = _strip_code(path.read_text(encoding="utf-8"))
    anchors, counts = set(), {}
    for match in re.finditer(r"^#{1,6}\s+(.*)$", text, flags=re.M):
        heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group(1).strip())
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        n = counts.get(slug, 0)
        counts[slug] = n + 1
        anchors.add(slug if n == 0 else f"{slug}-{n}")
    anchors.update(re.findall(r'<a id="([^"]+)"', text))
    return anchors


class RequiredDocsTests(unittest.TestCase):
    def test_required_files_exist(self):
        missing = [p for p in REQUIRED_FILES if not (REPO_ROOT / p).is_file()]
        self.assertEqual(missing, [])

    def test_claude_md_only_imports_agents_md(self):
        text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("@AGENTS.md"))
        self.assertLess(len(text.splitlines()), 10)

    def test_agents_md_stays_an_index(self):
        lines = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), AGENTS_MAX_LINES)
        text = "\n".join(lines)
        for guide in ("protocol-gap-work", "app-development", "image-appliance",
                      "releases-ci", "live-hardware-ops"):
            self.assertIn(f"docs/agents/{guide}.md", text)
            self.assertTrue((REPO_ROOT / "docs" / "agents" / f"{guide}.md").is_file())


class LinkTests(unittest.TestCase):
    def test_relative_links_and_anchors_resolve(self):
        broken = []
        for rel in _tracked("*.md"):
            if rel.startswith("_archive/"):
                continue
            source = REPO_ROOT / rel
            text = _strip_code(source.read_text(encoding="utf-8"))
            for link in re.findall(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)", text):
                if re.match(r"[a-z]+:", link):
                    continue
                target, _, anchor = link.partition("#")
                path = (source.parent / target).resolve() if target else source
                if target and not path.exists():
                    broken.append(f"{rel}: {link}")
                elif anchor and path.suffix == ".md" and anchor not in _anchors(path):
                    broken.append(f"{rel}: #{anchor}")
        self.assertEqual(broken, [], "broken links:\n" + "\n".join(broken))


class PublicationSafetyTests(unittest.TestCase):
    def test_no_personal_literals_or_leaked_identity(self):
        hits = []
        for rel in _tracked():
            try:
                text = (REPO_ROOT / rel).read_text(encoding="utf-8").lower()
            except (UnicodeDecodeError, OSError):
                continue
            for literal in LEAKED_IDENTITY:
                if literal in text:
                    hits.append(f"{rel}: leaked identity")
            if rel.startswith(PERSONAL_SCAN_EXEMPT):
                continue
            scrubbed = PUBLIC_HANDLE.sub("<owner>", text)
            for literal in PERSONAL_LITERALS:
                if literal in scrubbed:
                    hits.append(f"{rel}: personal literal")
        self.assertEqual(hits, [])

    def test_user_docs_do_not_overstate_acceptance(self):
        for rel in ("README.md", "docs/install.md", "docs/user-guide.md", "docs/status.md",
                    "docs/hardware.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8").lower()
            for claim in FORBIDDEN_ACCEPTANCE_CLAIMS:
                self.assertNotIn(claim, text, rel)

    def test_status_scopes_acceptance_to_the_tested_setup(self):
        text = (REPO_ROOT / "docs" / "status.md").read_text(encoding="utf-8").lower()
        self.assertIn("not yet recorded", text)
        self.assertIn("pi zero 2 w", text)


if __name__ == "__main__":
    unittest.main()
