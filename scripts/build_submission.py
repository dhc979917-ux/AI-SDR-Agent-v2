"""Build a source-only submission archive from an explicit allowlist."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ["README.md", "pyproject.toml", "requirements.txt", "requirements.lock",
         "run.sh", ".env.example", ".gitignore", ".gitattributes", "Dockerfile", ".dockerignore", "render.yaml", "render.paid.yaml", "docker-compose.cloudflare.yml", "docker-compose.vps.yml", "Caddyfile"]
DIRECTORIES = ["sdr", "static", "prompts", "data", "tests", "eval", "scripts", "docs", "cloudflare"]
EXCLUDED = {"__pycache__", ".pytest_cache", ".DS_Store", ".env"}


def main():
    paths = [ROOT / f for f in FILES]
    for directory in DIRECTORIES:
        paths.extend(p for p in (ROOT / directory).rglob("*") if p.is_file()
                     and not any(x in EXCLUDED for x in p.parts) and p.suffix not in (".pyc", ".pyo")
                     and not (p.parent.name == "cloudflare" and p.suffix == ".json"))
    paths = sorted(set(paths))
    if not all(p.exists() and not p.is_symlink() for p in paths):
        raise RuntimeError("Submission contains a missing file or symlink.")
    for p in paths:
        if p.parent == ROOT / 'eval/artifacts' and p.name.startswith(('asr-format-', 'audio-identity-', 'audio-roles-', 'long-audio-', 'personal-models-')):
            if json.loads(p.read_text()).get('data_mode') != 'synthetic':
                raise RuntimeError('ASR验收报告含非合成转写，请先移出交付目录：'+p.name)
    manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    output = ROOT.parent / "AI_SDR_Agent_作业交付包.zip"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, "ai-sdr-agent/" + str(p.relative_to(ROOT)))
        z.writestr("ai-sdr-agent/MANIFEST.sha256.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(output) as z:
        assert z.testzip() is None
        assert not any("/.venv/" in n or "/var/" in n or n.endswith("/.env") for n in z.namelist())
    print(json.dumps({"archive": str(output), "files": len(paths), "bytes": output.stat().st_size,
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
