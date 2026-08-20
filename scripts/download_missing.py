import json, os, re, time, urllib.request

MONTHS_PT = {}

def to_iso_date(br_date):
    d, m, y = br_date.split('/')
    return f"{y}-{m}-{d}"

def main():
    with open('diario-oficial-2026-08-20 (1).json', encoding='utf-8', errors='replace') as f:
        data = json.load(f)

    existing = os.listdir('diarios_oficiais')
    have = set()
    for fn in existing:
        m = re.search(r'edicao_(\d+)', fn)
        if m:
            have.add(int(m.group(1)))

    missing = [d for d in data if d['edicao'] not in have]
    missing.sort(key=lambda d: d['edicao'])
    print(f"Missing {len(missing)} editions")

    ok, fail = 0, 0
    for d in missing:
        edicao = d['edicao']
        iso = to_iso_date(d['data'])
        fname = f"diarios_oficiais/{iso}_edicao_{edicao:04d}.pdf"
        if os.path.exists(fname):
            continue
        url = d['arquivo']
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                content = resp.read()
            with open(fname, 'wb') as out:
                out.write(content)
            ok += 1
            print(f"OK {edicao} -> {fname} ({len(content)} bytes)")
        except Exception as e:
            fail += 1
            print(f"FAIL {edicao}: {e}")
        time.sleep(0.2)

    print(f"Done. ok={ok} fail={fail}")

if __name__ == '__main__':
    main()
