#!/usr/bin/env python3
"""
Analiza tracks nuevos (no en XML) y los añade a reloadtrack_cues.xml.
Modelo de CUE points:
  - INTRO:  siempre en bar 1 = 0.000s
  - DROP:   4 bars desde inicio (Rekordbox ajustará con su análisis)
  - OUTRO:  al 75% del track redondeado a frase de 4 bars
  - Comments: I:Xb | O:Yb | ZZZbpm
"""

import os, json, subprocess, re, shutil
import numpy as np
import soundfile as sf
from urllib.parse import unquote, quote
from datetime import date

XML_PATH  = "/Volumes/Musica/ReloadTrack/reloadtrack_cues.xml"
DISK_PATH = "/Volumes/Musica/ReloadTrack"
DISK_URL  = "X9%20Pro"
TODAY     = date.today().strftime("%Y-%m-%d")

FOLDERS = {
    "Remember":  {"genre": "Remember",  "colour": 4294944000},   # naranja
    "HouseMash": {"genre": "HouseMash", "colour": 4278255360},   # verde
}


# ─── BPM ──────────────────────────────────────────────────────────────────────

def detect_bpm(filepath):
    """BPM via autocorrelación sobre los primeros 30s."""
    try:
        audio, sr = sf.read(filepath, always_2d=True, dtype='float32')
        audio = audio[:, 0]
        ratio = max(1, sr // 11025)
        audio = audio[::ratio]; sr_d = sr // ratio
        audio = audio[:int(30 * sr_d)]

        hop = 256; nf = len(audio) // hop
        if nf < 50: return 0.0
        energy = np.array([np.sum(audio[i*hop:(i+1)*hop]**2) for i in range(nf)])
        onset  = np.maximum(0, np.diff(energy))
        n = len(onset)
        corr = np.correlate(onset, onset, 'full')[n-1:]
        fps = sr_d / hop
        min_p = max(1, int(fps * 60 / 195))
        max_p = int(fps * 60 / 70)
        if max_p >= len(corr): return 0.0
        peak = np.argmax(corr[min_p:max_p]) + min_p
        bpm = 60.0 * fps / peak
        if bpm < 90:  bpm *= 2
        elif bpm > 175: bpm /= 2
        return round(bpm, 2)
    except Exception as e:
        return 0.0


# ─── CUE POINTS (modelo simplificado) ─────────────────────────────────────────

def calc_cues(duration, bpm):
    """
    Devuelve (intro_bars, drop_bar, drop_sec, outro_bar, outro_sec, outro_bars).
    DROP: bar 5 (4 bars de intro estándar).
    OUTRO: 75% del track redondeado a frase de 4 bars.
    """
    if bpm <= 0:
        return 4, 5, 0.0, None, max(0, duration - 64.0), 32

    bar_dur    = 4 * 60.0 / bpm          # segundos por bar
    total_bars = round(duration / bar_dur)

    # DROP: bar 5 (intro de 4 bars)
    intro_bars = 4
    drop_bar   = 5
    drop_sec   = (drop_bar - 1) * bar_dur   # (bar-1) * bar_dur

    # OUTRO: al 75%, redondeado a múltiplo de 4 bars
    outro_bar_raw = round(total_bars * 0.75)
    outro_bar     = max(drop_bar + 8, (outro_bar_raw // 4) * 4 + 1)
    outro_sec     = (outro_bar - 1) * bar_dur
    outro_bars    = max(1, total_bars - outro_bar + 1)

    return intro_bars, drop_bar, drop_sec, outro_bar, outro_sec, outro_bars


# ─── FFPROBE ──────────────────────────────────────────────────────────────────

def get_meta(filepath):
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_format', '-show_streams', filepath],
            capture_output=True, text=True, timeout=15
        )
        d    = json.loads(r.stdout)
        fmt  = d.get('format', {})
        st   = d.get('streams', [{}])[0]
        tags = {k.upper(): v for k, v in fmt.get('tags', {}).items()}
        return dict(
            title       = tags.get('TITLE', ''),
            artist      = tags.get('ARTIST', ''),
            duration    = round(float(fmt.get('duration', 0))),
            bit_rate    = int(fmt.get('bit_rate', 0)) // 1000,
            sample_rate = int(st.get('sample_rate', 44100)),
        )
    except:
        return None


# ─── XML ──────────────────────────────────────────────────────────────────────

def xe(s):
    return s.replace('&','&amp;').replace('"','&quot;').replace('<','&lt;').replace('>','&gt;')

def make_entry(track_id, meta, genre, colour, folder, fname, bpm, cues):
    intro_bars, drop_bar, drop_sec, outro_bar, outro_sec, outro_bars = cues
    bpm_str = f"{bpm:.2f}" if bpm > 0 else "0.00"
    comment = f"I:{intro_bars}b | O:{outro_bars}b" if bpm > 0 else ""
    name    = meta['title'] or re.sub(r'\.flac$', '', fname, flags=re.I)
    loc     = f"file://localhost/Volumes/{DISK_URL}/Musica/ReloadTrack/{folder}/{quote(fname, safe='')}"

    lines = [
        f'    <TRACK TrackID="{track_id}" Name="{xe(name)}" Artist="{xe(meta["artist"])}" '
        f'Album="" Genre="{genre}" Kind="FLAC Audio File" Size="0" '
        f'TotalTime="{meta["duration"]}" DiscNumber="0" TrackNumber="0" Year="" '
        f'AverageBpm="{bpm_str}" DateAdded="{TODAY}" BitRate="{meta["bit_rate"]}" '
        f'SampleRate="{meta["sample_rate"]}" Comments="{comment}" PlayCount="0" '
        f'Rating="0" Location="{loc}" Colour="{colour}" '
        f'Remixer="" Tonality="" Label="" Mix="">',
        f'      <POSITION_MARK Name="INTRO {intro_bars}b" Type="0" Start="0.000" Num="0" '
        f'Red="0" Green="100" Blue="255" />',
        f'      <POSITION_MARK Name="DROP" Type="0" Start="{drop_sec:.3f}" Num="1" '
        f'Red="0" Green="220" Blue="50" />',
    ]
    if outro_bar:
        lines.append(
            f'      <POSITION_MARK Name="OUTRO {outro_bars}b" Type="0" Start="{outro_sec:.3f}" Num="2" '
            f'Red="255" Green="30" Blue="0" />'
        )
    lines.append('    </TRACK>')
    return '\n'.join(lines)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("Leyendo XML...")
    with open(XML_PATH, 'r', encoding='utf-8') as f:
        xml = f.read()

    existing = set(
        unquote(m.group(1).split('/')[-1]).lower()
        for m in re.finditer(r'Location="([^"]+)"', xml)
    )
    ids     = [int(m.group(1)) for m in re.finditer(r'TrackID="(\d+)"', xml)]
    next_id = max(ids) + 1 if ids else 1
    old_count = len(ids) // 2
    print(f"  Tracks en XML: {old_count} | Siguiente ID: {next_id}")

    new_entries = []
    errors      = []

    for folder, cfg in FOLDERS.items():
        fpath = os.path.join(DISK_PATH, folder)
        if not os.path.isdir(fpath):
            print(f"  ⚠️  {folder}: no existe"); continue

        flacs   = sorted(f for f in os.listdir(fpath)
                         if f.lower().endswith('.flac') and not f.startswith('._'))
        missing = [f for f in flacs if f.lower() not in existing]
        print(f"\n  {folder}: {len(missing)} nuevos de {len(flacs)}")

        for idx, fname in enumerate(missing, 1):
            fp = os.path.join(fpath, fname)
            short = fname[:52]
            print(f"  [{idx:3d}/{len(missing)}] {short:<52}", end=' ', flush=True)

            meta = get_meta(fp)
            if not meta:
                print("← skip (ffprobe falló)")
                errors.append(fname); continue

            bpm  = detect_bpm(fp)
            cues = calc_cues(meta['duration'], bpm)
            _, _, _, _, _, outro_bars = cues

            print(f"→ {meta['duration']}s BPM={bpm:.1f} O:{outro_bars}b", flush=True)

            entry = make_entry(
                track_id = next_id,
                meta     = meta,
                genre    = cfg['genre'],
                colour   = cfg['colour'],
                folder   = folder,
                fname    = fname,
                bpm      = bpm,
                cues     = cues,
            )
            new_entries.append(entry)
            next_id += 1

    if not new_entries:
        print("\nNada nuevo que añadir."); return

    print(f"\n✅ {len(new_entries)} entries generados. Actualizando XML...")

    new_block = '\n'.join(new_entries) + '\n'
    xml_new   = xml.replace('  </COLLECTION>', new_block + '  </COLLECTION>', 1)
    new_count = old_count + len(new_entries)
    xml_new   = re.sub(r'Entries="\d+"', f'Entries="{new_count}"', xml_new, count=1)

    shutil.copy2(XML_PATH, XML_PATH + ".bak")
    with open(XML_PATH, 'w', encoding='utf-8') as f:
        f.write(xml_new)

    print(f"  XML: {old_count} → {new_count} tracks")
    if errors:
        print(f"  ⚠️  {len(errors)} fallos: {errors[:5]}")
    print("  Backup: reloadtrack_cues.xml.bak")
    print("\n✔ Listo para importar en Rekordbox.")


if __name__ == '__main__':
    main()
