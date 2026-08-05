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


def test_statusline_writes_snapshot_and_renders(tmp_path):
    # 대시보드 '봇 세션' 클로드 카드의 데이터원 — statusLine 훅이 세션 스냅샷을 남긴다
    # (2026-08-05 E2E: 정본화 전엔 저자 로컬 사설 스크립트라 신규 계정에서 카드가 영구 공백)
    script = Path(__file__).parent.parent / "scripts/statusline-command.sh"
    payload = json.dumps({
        "session_id": "sess-1234",
        "workspace": {"current_dir": str(tmp_path / "discord-harness"),
                      "project_dir": str(tmp_path / "discord-harness")},
        "model": {"display_name": "Opus 5 (1M context)"},
        "context_window": {"used_percentage": 41.7},
    })
    r = subprocess.run(["bash", str(script)], input=payload, capture_output=True,
                       text=True, env=dict(os.environ, HOME=str(tmp_path)))
    assert r.returncode == 0, r.stderr
    assert "Opus 5 (1M context)" in r.stdout          # statusline 표시
    snap = json.loads((tmp_path / ".config/usage-coach/sessions/sess-1234.json").read_text())
    assert snap["model"] == "Opus 5 (1M context)"
    assert snap["project_dir"] == str(tmp_path / "discord-harness")
    assert snap["used"] == 41.7 and snap["ts"] > 0


def test_statusline_tolerates_bad_input(tmp_path):
    script = Path(__file__).parent.parent / "scripts/statusline-command.sh"
    r = subprocess.run(["bash", str(script)], input="not-json", capture_output=True,
                       text=True, env=dict(os.environ, HOME=str(tmp_path)))
    assert r.returncode == 0                          # statusline은 어떤 입력에도 죽지 않는다
