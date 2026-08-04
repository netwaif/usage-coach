import json, os, plistlib, subprocess
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"

def run(script, home):
    env = dict(os.environ, HOME=str(home), DRY_RUN="1")
    return subprocess.run(["bash", str(SCRIPTS / script)],
                          capture_output=True, text=True, env=env)

def _prep(home):
    cfg = home / ".config/usage-coach"; cfg.mkdir(parents=True)
    (cfg / "discord.json").write_text(json.dumps({"webhook_url": "https://example.invalid/hook"}))
    (home / "Library/LaunchAgents").mkdir(parents=True)

def test_install_writes_plist(tmp_path):
    _prep(tmp_path)
    r = run("install.sh", tmp_path)
    assert r.returncode == 0, r.stderr
    p = tmp_path / "Library/LaunchAgents/com.usage-coach.dashboard.plist"
    data = plistlib.loads(p.read_bytes())
    assert data["Label"] == "com.usage-coach.dashboard"
    assert data["StartInterval"] == 300
    assert data["ProgramArguments"][1].endswith("discord_dash.py")
    # launchd 기본 PATH엔 /usr/local/bin 이 없어 codexbar 호출이 죽는다 — 설치 시점 PATH 주입
    assert data["EnvironmentVariables"]["PATH"] == os.environ["PATH"]

def test_install_fails_without_webhook_config(tmp_path):
    (tmp_path / "Library/LaunchAgents").mkdir(parents=True)
    r = run("install.sh", tmp_path)
    assert r.returncode == 1
    assert "webhook" in r.stderr.lower() or "webhook" in r.stdout.lower()

def test_uninstall_removes_plist_and_keeps_config(tmp_path):
    _prep(tmp_path)
    run("install.sh", tmp_path)
    r = run("uninstall.sh", tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "Library/LaunchAgents/com.usage-coach.dashboard.plist").exists()
    assert (tmp_path / ".config/usage-coach/discord.json").exists()
