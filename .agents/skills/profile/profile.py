import json
import math
import sys
from pathlib import Path

import soundfile
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analyze"))
from analyze import LOUD_PARTS_LU, loudness, measure

LOSSLESS = {".flac", ".wav", ".aif", ".aiff"}
LOUD_FIELDS = ["bands_db", "side_mid_db", "side_mid_below_120hz_db"]
WHOLE_FIELDS = ["integrated_lufs", "max_short_term_lufs", "peak_to_loudness_db"]


def reference(path, rate):
    audio, source_rate = soundfile.read(path, dtype="float64", always_2d=True)
    # Resampling a clipped master raises its true peak, so loudness and dynamics use the original rate.
    whole, _, _ = loudness(audio, source_rate)
    if source_rate != rate:
        # Octave bands shift up to 0.8 dB between 44.1 and 48 kHz copies, so compare at the song's rate.
        common = math.gcd(rate, source_rate)
        audio = signal.resample_poly(audio, rate // common, source_rate // common, axis=0)
    loud = measure(audio, rate, loud=True)
    if loud["bands_db"] is None:
        sys.exit(f"{path}: no loud parts to measure")
    loud["bands_db"] = {k: v for k, v in loud["bands_db"].items() if k not in loud["partial_bands"]}
    return {"file": path.name} | {k: whole[k] for k in WHOLE_FIELDS} | {k: loud[k] for k in LOUD_FIELDS}


def build(profile_dir, rate):
    files = sorted(p for p in (profile_dir / "references").iterdir() if p.suffix.lower() in LOSSLESS)
    if not 3 <= len(files) <= 5:
        sys.exit(f"{profile_dir}/references: needs 3 to 5 lossless files, found {len(files)}")
    out = profile_dir / "profile.json"
    master = json.loads(out.read_text()).get("master_integrated_lufs") if out.exists() else None
    profile = {
        "sample_rate": rate,
        "loud_parts": f"3 s short-term windows within {LOUD_PARTS_LU:g} LU of the loudest",
        "master_integrated_lufs": master,
        "references": [reference(path, rate) for path in files],
    }
    out.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: uv run .agents/skills/profile/profile.py <profiles/name> <sample_rate>")
    print(build(Path(sys.argv[1]), int(sys.argv[2])))
