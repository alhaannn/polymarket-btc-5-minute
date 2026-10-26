import json
import tarfile
from pathlib import Path

from polybot.config import PolybotConfig, RecorderConfig
from polybot.recorder import WindowWriter, MarketRecorder


def test_window_writer(tmp_path: Path):
    writer = WindowWriter(tmp_path)

    cl_tick = {"received_at_ms": 1700000001000, "feed": "chainlink", "price": 80100.5}
    bn_tick = {"received_at_ms": 1700000001050, "feed": "binance", "price": 80102.0}
    mkt_event = {"received_at_ms": 1700000001100, "price_changes": []}
    metadata = {"slug": "btc-updown-5m-1700000000", "window_start": 1700000000, "K": 80100.5}
    resolution = {"slug": "btc-updown-5m-1700000000", "winner": "Up"}

    writer.write_chainlink(cl_tick)
    writer.write_binance(bn_tick)
    writer.write_market(mkt_event)
    writer.write_metadata(metadata)
    writer.write_resolution(resolution)
    writer.close()

    # Verify content
    with open(tmp_path / "chainlink.jsonl", "r", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f]
        assert len(lines) == 1
        assert lines[0]["price"] == 80100.5

    with open(tmp_path / "binance.jsonl", "r", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f]
        assert len(lines) == 1
        assert lines[0]["price"] == 80102.0

    with open(tmp_path / "metadata.json", "r", encoding="utf-8") as f:
        meta_read = json.load(f)
        assert meta_read["slug"] == "btc-updown-5m-1700000000"

    with open(tmp_path / "resolution.json", "r", encoding="utf-8") as f:
        res_read = json.load(f)
        assert res_read["winner"] == "Up"


def test_rotate_and_compress(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True)

    cfg = PolybotConfig()
    cfg.recorder.raw_dir = raw_dir
    cfg.recorder.manifest_file = raw_dir / "manifest.json"
    cfg.recorder.max_uncompressed_windows = 2  # Keep 2 uncompressed

    recorder = MarketRecorder(cfg)

    # Create 4 dummy windows
    for i in range(4):
        w_start = 1700000000 + i * 300
        wdir = raw_dir / str(w_start)
        wdir.mkdir()
        with open(wdir / "metadata.json", "w", encoding="utf-8") as f:
            json.dump({"window_start": w_start}, f)
        with open(wdir / "resolution.json", "w", encoding="utf-8") as f:
            json.dump({"winner": "Up" if i % 2 == 0 else "Down"}, f)

    # Rotate
    recorder._rotate_and_compress()

    # Out of 4 windows, first 2 should be compressed into .tar.gz and dir deleted
    w0_tar = raw_dir / "1700000000.tar.gz"
    w1_tar = raw_dir / "1700000300.tar.gz"
    assert w0_tar.exists()
    assert w1_tar.exists()
    assert not (raw_dir / "1700000000").exists()
    assert not (raw_dir / "1700000300").exists()

    # The last 2 uncompressed remain as directories
    assert (raw_dir / "1700000600").is_dir()
    assert (raw_dir / "1700000900").is_dir()

    # Manifest should record compressed windows
    assert cfg.recorder.manifest_file.exists()
    with open(cfg.recorder.manifest_file, "r", encoding="utf-8") as f:
        manifest = json.load(f)
        assert "1700000000" in manifest["windows"]
        assert manifest["windows"]["1700000000"]["winner"] == "Up"
        assert manifest["windows"]["1700000300"]["winner"] == "Down"
