"""Job queue: lifecycle, FIFO with one worker, cancel, error mapping, outputs, history."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from webutil import wait_final, wait_for, write_media

from subfix import device
from subfix.web import jobs as jobs_mod


def submit(client, media, **kw):
    body = {"media": {"path": str(media)}, **kw}
    r = client.post("/api/jobs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_job_lifecycle_generate(client, env):
    media = write_media(env, "Tears.mov")
    job = submit(client, media, mode="auto", language="en")
    assert job["id"].startswith("j-")
    assert job["state"] in ("queued", "running")
    assert job["media"] == {
        "path": os.path.abspath(media),
        "name": "Tears.mov",
        "kind": "video",
        "size_bytes": 64,
        "duration_seconds": 12.5,
        "source": "path",
    }
    assert job["options"] == {
        "mode": "auto",
        "sync_mode": "auto",
        "translate": False,
        "language": "en",
    }
    final = wait_final(client, job["id"])
    assert final["state"] == "done", final
    assert final["error"] is None
    assert final["queue_position"] is None
    assert final["progress"]["overall"] == 1.0
    res = final["result"]
    out = media.with_name("Tears.generated.srt")
    assert res["output_path"] == str(out)
    assert res["output_name"] == "Tears.generated.srt"
    assert res["kind"] == "generated" and res["method"] == "generate"
    assert res["cues"] == 1 and res["language"] == "en"
    assert res["preview"][0]["text"] == "Hello there."
    assert res["elapsed_seconds"] >= 0
    assert out.is_file()
    # the fake was called with the request options, output next to the original
    call = env["runner"].calls[0]
    assert call["out"] is None and call["language"] == "en" and call["mode"] == "auto"
    # log captured with rich markup stripped
    msgs = [line["message"] for line in final["log_tail"]]
    assert "Processing Tears.mov now" in msgs
    assert final["log_tail"][0]["job_id"] == job["id"]
    # listed and recorded in history
    listed = client.get("/api/jobs").json()["jobs"]
    assert [j["id"] for j in listed] == [job["id"]]
    assert "log_tail" not in listed[0]
    items = client.get("/api/history").json()["items"]
    assert items[0]["id"] == job["id"]
    assert items[0]["state"] == "done"
    assert items[0]["output_path"] == str(out)
    assert items[0]["media_duration_seconds"] == 12.5
    assert items[0]["summary"] == res["summary"]
    # download + open folder
    r = client.get(f"/api/jobs/{job['id']}/srt")
    assert r.status_code == 200
    assert "Hello there." in r.text
    assert r.headers["content-disposition"].startswith("attachment")
    assert "Tears.generated.srt" in r.headers["content-disposition"]
    assert client.post(f"/api/jobs/{job['id']}/open-folder").json() == {"ok": True}
    assert env["opened"].calls == [(out,)]


def test_sync_with_sidecar_and_explicit_subs(client, env):
    media = write_media(env, "Movie.mkv")
    r = client.post("/api/jobs", json={"media": {"path": str(media)}, "mode": "sync"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "no_subtitles"
    side = env["media"] / "Movie.srt"
    side.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n\n")
    job = submit(client, media, mode="sync", sync_mode="smart")
    final = wait_final(client, job["id"])
    assert final["state"] == "done"
    assert final["result"]["output_name"] == "Movie.synced.srt"
    assert final["result"]["notes"] == ["a note"]
    other = env["media"] / "other.ass"
    other.write_text("x")
    job = submit(client, media, subs={"path": str(other)}, mode="sync")
    assert job["subs"] == {"path": os.path.abspath(other), "name": "other.ass", "source": "path"}
    wait_final(client, job["id"])
    assert env["runner"].calls[-1]["subs_path"] == Path(os.path.abspath(other))


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"media": {}}, 400, "invalid_request"),
        ({"media": {"path": "X", "upload_id": "u-1"}}, 400, "invalid_request"),
        ({"media": {"upload_id": "u-00000000"}}, 404, "upload_not_found"),
        ({"media": {"path": "MEDIA"}, "mode": "dance"}, 400, "invalid_request"),
        ({"media": {"path": "MEDIA"}, "sync_mode": "fast"}, 400, "invalid_request"),
        ({"media": {"path": "MEDIA"}, "language": "xx"}, 400, "invalid_request"),
        ({"media": {"path": "SUBS"}}, 400, "unsupported_format"),
        ({"media": {"path": "MEDIA"}, "subs": {"path": "MEDIA"}}, 400, "unsupported_format"),
        ({"media": {"path": "TXT"}}, 400, "unsupported_format"),
        ({"media": {"path": "MISSING"}}, 404, "file_not_found"),
        ({"media": {"path": "MEDIA"}, "translate": "maybe"}, 400, "invalid_request"),
    ],
)
def test_create_job_validation(client, env, body, status, code):
    paths = {
        "MEDIA": str(write_media(env, "m.mp4")),
        "SUBS": str(write_media(env, "s.srt")),
        "TXT": str(write_media(env, "t.txt")),
        "MISSING": str(env["media"] / "gone.mkv"),
    }

    def fill(obj):
        if isinstance(obj, dict):
            return {k: fill(v) for k, v in obj.items()}
        return paths.get(obj, obj) if isinstance(obj, str) else obj

    r = client.post("/api/jobs", json=fill(body))
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code


def test_empty_language_means_auto(client, env):
    job = submit(client, write_media(env, "a.mp3"), language="")
    assert job["options"]["language"] is None
    wait_final(client, job["id"])


def test_upload_job_output_and_cleanup(client, env, app):
    up = client.post("/api/uploads?filename=Clip.webm&size=5", content=b"12345").json()
    subs = client.post("/api/uploads?filename=Clip.vtt&size=3", content=b"abc").json()
    job = client.post(
        "/api/jobs",
        json={"media": {"upload_id": up["upload_id"]}, "subs": {"upload_id": subs["upload_id"]}},
    ).json()
    assert job["media"]["source"] == "upload"
    assert job["subs"]["source"] == "upload"
    final = wait_final(client, job["id"])
    assert final["state"] == "done"
    out = Path(final["result"]["output_path"])
    assert out == app.state.outputs_dir / job["id"] / "Clip.synced.srt"
    assert out.is_file()
    # uploads are deleted once the job ended
    wait_for(lambda: not Path(up["path"]).exists() and not Path(subs["path"]).exists())
    assert client.delete(f"/api/uploads/{up['upload_id']}").status_code == 404


def test_upload_generate_output_location(client, env, app):
    up = client.post("/api/uploads?filename=Talk.m4a&size=5", content=b"12345").json()
    job = client.post("/api/jobs", json={"media": {"upload_id": up["upload_id"]}}).json()
    final = wait_final(client, job["id"])
    out = app.state.outputs_dir / job["id"] / "Talk.generated.srt"
    assert final["result"]["output_path"] == str(out)


def test_unwritable_folder_falls_back(client, env, app, monkeypatch):
    monkeypatch.setattr(jobs_mod, "_folder_writable", lambda folder: False)
    job = submit(client, write_media(env, "ro.mkv"))
    final = wait_final(client, job["id"])
    out = app.state.outputs_dir / job["id"] / "ro.generated.srt"
    assert final["result"]["output_path"] == str(out)
    assert any("not writable" in n for n in final["result"]["notes"])


def test_queue_runs_one_at_a_time_fifo(client, env):
    runner = env["runner"]
    ids = [submit(client, write_media(env, f"gate{i}.mkv"))["id"] for i in range(3)]
    wait_for(lambda: client.get(f"/api/jobs/{ids[0]}").json()["state"] == "running")
    jobs = {j["id"]: j for j in client.get("/api/jobs").json()["jobs"]}
    assert [j["id"] for j in client.get("/api/jobs").json()["jobs"]] == ids
    assert jobs[ids[0]]["queue_position"] is None
    assert jobs[ids[1]]["state"] == "queued" and jobs[ids[1]]["queue_position"] == 1
    assert jobs[ids[2]]["queue_position"] == 2
    runner.gate.set()
    finals = [wait_final(client, i) for i in ids]
    assert all(f["state"] == "done" for f in finals)
    assert runner.max_active == 1
    assert runner.order == ["gate0", "gate1", "gate2"]
    for a, b in zip(finals, finals[1:], strict=False):
        assert a["finished_at"] <= b["started_at"]
    listed = [j["id"] for j in client.get("/api/jobs").json()["jobs"]]
    assert listed == list(reversed(ids))  # finished: newest first


def test_cancel_queued_and_running(client, env):
    running = submit(client, write_media(env, "loop.mkv"))
    queued = submit(client, write_media(env, "after.mkv"))
    wait_for(lambda: client.get(f"/api/jobs/{running['id']}").json()["state"] == "running")
    r = client.post(f"/api/jobs/{queued['id']}/cancel")
    assert r.status_code == 200
    assert r.json()["state"] == "cancelled"
    r = client.post(f"/api/jobs/{running['id']}/cancel")
    assert r.status_code == 200
    assert r.json()["state"] in ("running", "cancelled")
    final = wait_final(client, running["id"])
    assert final["state"] == "cancelled"
    assert final["error"] is None and final["result"] is None
    r = client.post(f"/api/jobs/{running['id']}/cancel")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "job_finished"
    assert "after" not in env["runner"].order  # the cancelled queued job never ran
    states = {i["id"]: i["state"] for i in client.get("/api/history").json()["items"]}
    assert states == {running["id"]: "cancelled", queued["id"]: "cancelled"}
    r = client.get(f"/api/jobs/{running['id']}/srt")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "output_not_found"
    assert client.post(f"/api/jobs/{running['id']}/open-folder").status_code == 404


@pytest.mark.parametrize(
    ("stem", "code"),
    [
        ("fail_audio", "audio_error"),
        ("fail_subs", "subtitle_error"),
        ("fail_model", "model_error"),
        ("fail_nosubs", "no_subtitles"),
        ("fail_internal", "internal"),
    ],
)
def test_failures_map_to_error_codes(client, env, stem, code):
    job = submit(client, write_media(env, f"{stem}.mkv"))
    final = wait_final(client, job["id"])
    assert final["state"] == "failed"
    assert final["error"]["code"] == code
    assert final["error"]["message"]
    assert final["result"] is None
    item = client.get("/api/history").json()["items"][0]
    assert item["state"] == "failed" and item["error"]["code"] == code


def test_unknown_job(client):
    for method, path in [
        ("get", "/api/jobs/j-nope"),
        ("post", "/api/jobs/j-nope/cancel"),
        ("get", "/api/jobs/j-nope/srt"),
        ("post", "/api/jobs/j-nope/open-folder"),
    ]:
        r = getattr(client, method)(path)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "job_not_found"


def test_history_limit_and_clear(client, env):
    for i in range(3):
        wait_final(client, submit(client, write_media(env, f"h{i}.wav"))["id"])
    items = client.get("/api/history?limit=2").json()["items"]
    assert [i["name"] for i in items] == ["h2.wav", "h1.wav"]
    out = Path(items[0]["output_path"])
    assert client.delete("/api/history").json() == {"ok": True}
    assert client.get("/api/history").json()["items"] == []
    assert out.is_file()  # clearing history never deletes subtitles
    assert client.get("/api/history?limit=0").status_code == 400


def test_progress_events_throttled_monotonic(client, env, app, monkeypatch):
    events: list[tuple[float, dict]] = []
    broker = app.state.broker
    orig = broker.publish

    def spy(event, data):
        if event == "job":
            events.append((time.monotonic(), data))
        orig(event, data)

    monkeypatch.setattr(broker, "publish", spy)
    job = submit(client, write_media(env, "many.mkv"))
    wait_final(client, job["id"])
    mine = [(t, d) for t, d in events if d["id"] == job["id"]]
    running = [(t, d) for t, d in mine if d["state"] == "running"]
    # 200+ progress reports in ~0.5 s collapse to a handful of events (<= 4/s + state changes)
    span = running[-1][0] - running[0][0]
    assert len(running) <= span * 4 + 3, (len(running), span)
    overall = [d["progress"]["overall"] for _, d in mine]
    assert overall == sorted(overall)
    assert mine[-1][1]["state"] == "done" and overall[-1] == 1.0
    stages = {d["progress"]["stage"] for _, d in running}
    assert "transcribe" in stages


def test_overall_and_eta_computation(env):
    job = jobs_mod.Job(
        "j-1",
        jobs_mod.FileSpec(Path("x.mkv"), "x.mkv", "video", "path"),
        None,
        "auto",
        "auto",
        False,
        None,
    )

    class Broker:
        def publish(self, *a):
            pass

    m = jobs_mod.JobManager(Path("."), None, None, Broker(), throttle_seconds=0)
    job.t_started = time.monotonic() - 10
    m._on_progress(job, "extract", 0.5, "")
    assert job.overall == pytest.approx(0.025)
    assert job.eta_seconds == pytest.approx(10 * 0.975 / 0.025, rel=0.01)
    m._on_progress(job, "vad", 1.0, "")
    assert job.overall == pytest.approx(0.12)
    m._on_progress(job, "sync", None, "sync mode: smart")  # indeterminate: no jump to 95 %
    assert job.overall == pytest.approx(0.12)
    assert job.stage == "sync" and job.stage_progress is None
    m._on_progress(job, "transcribe", 0.5, "")
    assert job.overall == pytest.approx(0.15 + 0.4)
    m._on_progress(job, "extract", 0.1, "")  # never goes backwards
    assert job.overall == pytest.approx(0.55)
    m._on_progress(job, "write", 1.0, "")
    assert job.overall < 1.0  # 1.0 only once the job is done
    fresh = jobs_mod.Job("j-2", job.media, None, "auto", "auto", False, None)
    fresh.t_started = time.monotonic()
    m._on_progress(fresh, "extract", 0.5, "")
    assert fresh.eta_seconds is None  # no estimate in the first second


def test_models_kept_then_released_when_idle(client, env, app):
    assert device.keep_models_loaded is True
    wait_final(client, submit(client, write_media(env, "idle.mkv"))["id"])
    wait_for(lambda: len(env["released"].calls) == 1, timeout=5)
    time.sleep(0.5)
    assert len(env["released"].calls) == 1  # only once per idle period


def test_keep_models_flag_restored_after_shutdown(env):
    from fastapi.testclient import TestClient
    from webutil import PORT, make_app

    before = device.keep_models_loaded
    with TestClient(make_app(env), base_url=f"http://127.0.0.1:{PORT}"):
        assert device.keep_models_loaded is True
    assert device.keep_models_loaded is before


def test_shutdown_cancels_running_and_queued(env):
    from fastapi.testclient import TestClient
    from webutil import PORT, TOKEN, make_app

    app = make_app(env)
    with TestClient(
        app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-Subfix-Token": TOKEN}
    ) as c:
        a = submit(c, write_media(env, "loop_a.mkv"))
        b = submit(c, write_media(env, "b.mkv"))
        wait_for(lambda: c.get(f"/api/jobs/{a['id']}").json()["state"] == "running")
    jm = app.state.jobs
    assert jm.get(a["id"]).state == "cancelled"
    assert jm.get(b["id"]).state == "cancelled"
    assert {i["id"] for i in app.state.history.items()} == {a["id"], b["id"]}


def test_map_error_unit():
    from subfix import formats

    assert jobs_mod.map_error(formats.UnsupportedFormat("x"))[0] == "unsupported_format"
    assert jobs_mod.map_error(ValueError("bad option"))[0] == "internal"
    assert jobs_mod.map_error(OSError("disk"))[0] == "internal"
