"""
Minimal FIT parser for Garmin golf activity files.
Extracts session-level summary + per-sample GPS/altitude track.

Returns a dict with:
  Session (group-safe):
    sport, start_time, total_distance_m, total_elapsed_s, total_timer_s,
    total_ascent_m, total_descent_m, avg_speed_ms, max_speed_ms,
    start_pos (lat,lng), nec (lat,lng), swc (lat,lng)
  Session (store-not-show):
    total_calories, avg_heart_rate, max_heart_rate
  Track:
    records — list of {t, lat, lng, alt_m, dist_m, hr} per GPS sample
    (only every Nth point kept — configurable)

FIT format summary:
  Header (12 or 14 bytes) [data...] CRC(2)
  Data records start with a 1-byte header:
    bit7 (0x80) = compressed timestamp record (rare, uses existing def)
    bit6 (0x40) = definition record
    bit5 (0x20) = developer-data flag
    bits 0-3    = local message type (0-15)
"""

import struct
from datetime import datetime, timedelta, timezone

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
SEMI_TO_DEG = 180.0 / (2**31)

SPORT_NAMES = {
    0:'generic', 1:'running', 2:'cycling', 3:'transition', 4:'fitness_equipment',
    5:'swimming', 11:'walking', 17:'hiking', 25:'golf', 35:'snowshoeing',
}

BASE_SIZE = {0x00:1, 0x01:1, 0x02:1, 0x83:2, 0x84:2, 0x85:4, 0x86:4,
             0x07:1, 0x88:4, 0x89:8, 0x0a:1, 0x8b:2, 0x8c:4, 0x0d:1}
BASE_FMT  = {0x00:'B', 0x01:'b', 0x02:'B', 0x83:'h', 0x84:'H', 0x85:'i',
             0x86:'I', 0x88:'f', 0x89:'d', 0x0a:'B', 0x8b:'H', 0x8c:'I', 0x0d:'B'}
INVALID   = {0x00:0xFF, 0x01:0x7F, 0x02:0xFF,
             0x83:0x7FFF, 0x84:0xFFFF,
             0x85:0x7FFFFFFF, 0x86:0xFFFFFFFF,
             0x0a:0x00, 0x8b:0x0000, 0x8c:0x00000000}


def _iter_records(raw):
    """Yield (gmn, {field_num: value}) for every data record."""
    hdr_sz = raw[0]
    if hdr_sz not in (12, 14) or raw[8:12] != b'.FIT':
        return
    data_sz = int.from_bytes(raw[4:8], 'little')
    pos = hdr_sz
    end = hdr_sz + data_sz
    defs = {}  # local_type -> (gmn, [(fn, sz, bt)], endian)

    while pos < end:
        hdr = raw[pos]
        pos += 1
        if hdr & 0x80:
            lt = (hdr >> 5) & 0x03
            if lt in defs:
                pos += sum(sz for _, sz, _ in defs[lt][1])
            continue
        lt = hdr & 0x0F
        if hdr & 0x40:  # definition
            pos += 1
            arch = raw[pos]; pos += 1
            endian = '<' if arch == 0 else '>'
            gmn = struct.unpack(endian + 'H', raw[pos:pos+2])[0]; pos += 2
            nf = raw[pos]; pos += 1
            fields = []
            for _ in range(nf):
                fields.append((raw[pos], raw[pos+1], raw[pos+2]))
                pos += 3
            if hdr & 0x20:
                nd = raw[pos]; pos += 1
                pos += nd * 3
            defs[lt] = (gmn, fields, endian)
            continue

        if lt not in defs:
            return
        gmn, fields, endian = defs[lt]
        values = {}
        for fn, sz, bt in fields:
            chunk = raw[pos:pos+sz]; pos += sz
            bs = BASE_SIZE.get(bt); fm = BASE_FMT.get(bt)
            if not bs or not fm or sz < bs:
                continue
            try:
                (v,) = struct.unpack(endian + fm, chunk[:bs])
                if v != INVALID.get(bt):
                    values[fn] = v
            except struct.error:
                pass
        yield gmn, values


def parse_fit_round(path, alt_sample_every=20):
    """
    Parse a golf FIT file and return a dict with session summary + GPS/altitude track.

    alt_sample_every: keep every Nth record message. 20 ≈ 1 sample per 20 seconds
    for typical 1-Hz golf watches. Set to 1 for all, to 1_000_000 to skip track.
    """
    with open(path, 'rb') as f:
        raw = f.read()

    session = None
    records = []
    start_t = None
    rec_count = 0

    for gmn, vals in _iter_records(raw):
        if gmn == 18 and session is None:
            session = vals
            start_t = vals.get(2)
        elif gmn == 20:
            rec_count += 1
            if rec_count % alt_sample_every != 0:
                continue
            t = vals.get(253)
            lat_s = vals.get(0)
            lng_s = vals.get(1)
            alt_raw = vals.get(2)
            dist_cm = vals.get(5)
            hr = vals.get(3)
            if alt_raw is None:
                continue
            records.append({
                't_off':  (t - start_t) if (t and start_t) else None,
                'lat':    lat_s * SEMI_TO_DEG if lat_s is not None else None,
                'lng':    lng_s * SEMI_TO_DEG if lng_s is not None else None,
                'alt_m':  alt_raw / 5.0 - 500.0,
                'dist_m': dist_cm / 100.0 if dist_cm is not None else None,
                'hr':     hr,
            })

    if not session:
        return None

    sport_id = session.get(5)
    start_raw = session.get(2)

    def s(key, scale=None):
        v = session.get(key)
        if v is None: return None
        return (v / scale) if scale else v

    return {
        'sport':            SPORT_NAMES.get(sport_id, f'unknown_{sport_id}'),
        'sport_id':         sport_id,
        'start_time':       (FIT_EPOCH + timedelta(seconds=start_raw)) if start_raw else None,
        'total_distance_m': s(9, 100),
        'total_elapsed_s':  s(7, 1000),
        'total_timer_s':    s(8, 1000),
        'total_ascent_m':   session.get(22),
        'total_descent_m':  session.get(23),
        'avg_speed_ms':     s(14, 1000),
        'max_speed_ms':     s(15, 1000),
        'start_lat':        session.get(3) * SEMI_TO_DEG if session.get(3) is not None else None,
        'start_lng':        session.get(4) * SEMI_TO_DEG if session.get(4) is not None else None,
        'nec_lat':          session.get(29) * SEMI_TO_DEG if session.get(29) is not None else None,
        'nec_lng':          session.get(30) * SEMI_TO_DEG if session.get(30) is not None else None,
        'swc_lat':          session.get(31) * SEMI_TO_DEG if session.get(31) is not None else None,
        'swc_lng':          session.get(32) * SEMI_TO_DEG if session.get(32) is not None else None,
        'total_calories':   session.get(11),
        'avg_heart_rate':   session.get(16),
        'max_heart_rate':   session.get(17),
        'records':          records,
    }


# Back-compat alias for earlier callers
def parse_fit_session(path):
    r = parse_fit_round(path, alt_sample_every=1_000_000)
    if r:
        r.pop('records', None)
    return r


if __name__ == '__main__':
    import sys
    path = sys.argv[1]
    r = parse_fit_round(path, alt_sample_every=30)
    if not r:
        print("Failed to parse"); sys.exit(1)
    def m(v): return f'{v:.0f}' if v is not None else '?'
    print(f"Sport: {r['sport']}  Start: {r['start_time']}")
    print(f"Distance: {(r['total_distance_m'] or 0)/1000:.2f}km  "
          f"Elapsed: {(r['total_elapsed_s'] or 0)/60:.0f}min")
    print(f"Ascent: {m(r['total_ascent_m'])}m  Descent: {m(r['total_descent_m'])}m")
    print(f"Speed avg/max: {m((r['avg_speed_ms'] or 0)*3.6)}/{m((r['max_speed_ms'] or 0)*3.6)} km/h")
    print(f"HR avg/max: {r['avg_heart_rate']}/{r['max_heart_rate']}   Cal: {r['total_calories']}")
    print(f"Records sampled: {len(r['records'])}")
    if r['records']:
        alts = [x['alt_m'] for x in r['records']]
        print(f"Altitude range: {min(alts):.0f}–{max(alts):.0f}m ({max(alts)-min(alts):.0f}m span)")
