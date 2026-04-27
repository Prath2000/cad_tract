"""
DWG → GeoJSON Executor

Reads DWGs directly from open AutoCAD instance via COM.
Supports single-block DWG and master DWG (all blocks).

match_mode: exact  → one layer  → Block_No from spatial join or ID
match_mode: prefix → all layers → Block_No from layer name suffix

Usage:
    python executor.py config.yaml
    python executor.py config.yaml --layers "Layer Name"
    python executor.py config.yaml --list
    python executor.py config.yaml --all

Dependencies:
    pip install pywin32 pyyaml
"""

import os, sys, json, math, re, difflib, argparse, yaml, time

try:
    import win32com.client
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

try:
    from shapely.geometry import Polygon as ShapelyPolygon, MultiPolygon
    from shapely.ops import unary_union
    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False

# Pre-compiled regexes used in the hot path (feature-level loops)
_RE_HAS_SUBSUFFIX = re.compile(r'[a-z]$')          # 'A9a' yes, 'A9' no
_RE_PLOT_PREFIX   = re.compile(r'^([A-Za-z]\d+[a-z]?)')  # 'A9a_BLK01' → 'A9a'


# ============================================================
# LOGGING
# ============================================================

class Logger:
    @staticmethod
    def ok(msg):       print(f"  \u2713  {msg}")
    @staticmethod
    def warn(msg):     print(f"  \u26a0  {msg}")
    @staticmethod
    def err(msg):      print(f"  \u2717  {msg}")
    @staticmethod
    def info(msg):     print(f"     {msg}")
    @staticmethod
    def section(msg):  print(f"\n{'='*60}\n  {msg}\n{'='*60}")
    @staticmethod
    def step(msg):     print(f"\n\u2500\u2500 {msg}")
    _progress_start = {}   # label -> start time

    # ── Live dual-bar progress state ─────────────────────────────────────────
    # Updated by progress() calls; rendered live by _progress_thread at ~4Hz.
    _live = {
        "entity_current": 0, "entity_total": 0, "entity_label": "",
        "entity_start":   0.0,
        "layer_current":  0, "layer_total":  0,
        "layer_start":    0.0, "layer_avg_s": 0.0,
        "active": False,
    }
    _progress_thread = None
    _progress_lock   = None

    @staticmethod
    def _fmt_time(secs):
        if secs >= 3600:
            return f"{int(secs//3600)}h {int((secs%3600)//60)}m"
        if secs >= 60:
            return f"{int(secs//60)}m {int(secs%60)}s"
        return f"{secs:.0f}s"

    @staticmethod
    def _render_bar(current, total, width=40):
        pct    = current / total if total > 0 else 0
        filled = int(pct * width)
        return "\u2588" * filled + "\u2591" * (width - filled), int(pct * 100)

    @staticmethod
    def _live_printer():
        """Background thread — redraws dual progress bars at ~4 Hz."""
        import threading
        while Logger._live["active"]:
            now = time.time()
            lv  = Logger._live

            # ── Entity bar ───────────────────────────────────────────────────
            ec, et = lv["entity_current"], lv["entity_total"]
            if et > 0:
                ebar, epct = Logger._render_bar(ec, et, 35)
                e_elapsed  = now - lv["entity_start"] if lv["entity_start"] else 0
                if ec > 1 and e_elapsed > 0:
                    e_rate = ec / e_elapsed
                    e_rem  = (et - ec) / e_rate if e_rate > 0 else 0
                    e_eta  = Logger._fmt_time(e_rem)
                else:
                    e_eta = "…"
                e_line = (f"  [{ebar}] {epct:3d}%  {ec}/{et}  "
                          f"{Logger._fmt_time(e_elapsed)} elapsed  {e_eta} left"
                          f"  {lv['entity_label']}")
            else:
                e_line = ""

            # ── Layer bar ────────────────────────────────────────────────────
            lc, lt = lv["layer_current"], lv["layer_total"]
            if lt > 0:
                lbar, lpct = Logger._render_bar(lc, lt, 35)
                l_elapsed  = now - lv["layer_start"] if lv["layer_start"] else 0
                avg        = lv["layer_avg_s"]
                if avg > 0 and lc > 0:
                    l_rem = avg * (lt - lc)
                    l_eta = Logger._fmt_time(l_rem)
                else:
                    l_eta = "…"
                l_line = (f"  [{lbar}] {lpct:3d}%  layer {lc}/{lt}  "
                          f"{Logger._fmt_time(l_elapsed)} elapsed  {l_eta} left")
            else:
                l_line = ""

            # Print both lines, overwriting previous output
            # \033[2A moves cursor up 2 lines; \033[K clears to end of line
            if e_line or l_line:
                out = ""
                if l_line:
                    out += f"\r\033[K{l_line}\n"
                if e_line:
                    out += f"\r\033[K{e_line}"
                # Move cursor back up by number of lines printed minus 1
                n_up = (1 if l_line else 0)
                if n_up:
                    out += f"\033[{n_up}A"
                print(out, end="", flush=True)

            time.sleep(0.25)

    @staticmethod
    def start_live_progress(total_layers, layer_start_time):
        """Start background rendering thread for the dual progress bars."""
        import threading
        Logger._live.update({
            "layer_current": 0, "layer_total": total_layers,
            "layer_start":   layer_start_time, "layer_avg_s": 0.0,
            "entity_current": 0, "entity_total": 0, "entity_label": "",
            "entity_start":  0.0,
            "active": True,
        })
        t = threading.Thread(target=Logger._live_printer, daemon=True)
        t.start()
        Logger._progress_thread = t
        # Reserve two lines for the bars
        print("\n\n", end="", flush=True)

    @staticmethod
    def stop_live_progress():
        """Stop background thread and clear bar lines."""
        Logger._live["active"] = False
        if Logger._progress_thread:
            Logger._progress_thread.join(timeout=0.6)
        # Clear the two bar lines
        print(f"\r\033[K\033[1A\r\033[K", end="", flush=True)

    @staticmethod
    def update_layer_progress(current, total, avg_secs=0.0):
        """Called at start of each layer to advance the layer bar."""
        Logger._live["layer_current"] = current
        Logger._live["layer_total"]   = total
        Logger._live["layer_avg_s"]   = avg_secs
        # Reset entity bar for the new layer
        Logger._live["entity_current"] = 0
        Logger._live["entity_total"]   = 0
        Logger._live["entity_label"]   = ""
        Logger._live["entity_start"]   = 0.0

    @staticmethod
    def progress(current, total, label=""):
        """Update entity-level progress state (read by background thread)."""
        now = time.time()
        if current == 1:
            Logger._live["entity_start"] = now
            Logger._progress_start[label] = now
        Logger._live["entity_current"] = current
        Logger._live["entity_total"]   = total
        Logger._live["entity_label"]   = label
        # On completion — let the bar show 100% briefly, then clear entity state
        if current == total:
            elapsed_total = now - Logger._progress_start.get(label, now)
            Logger._progress_start.pop(label, None)


# ============================================================
# CONFIG
# ============================================================

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ============================================================
# PLOT DETECTOR
# ============================================================

def detect_plot_from_path(path):
    """Extract plot ID from DWG filename eg 'P01' from 'Layout (Plot P01)'"""
    fname    = os.path.basename(path)
    patterns = [
        r'[Pp]lot\s+([A-Za-z]\d+[a-z]?)',      # "Plot A9a"
        r'[Rr]outing\s+([A-Za-z]\d+[a-z]?)',    # "Routing A9a"
        r'[Bb]oundary([A-Za-z]\d+[a-z]?)',        # "BlockBoundaryA9a"
        r'[Bb]lock[A-Za-z]*([A-Za-z]\d+[a-z]?)', # "BlockA9a"
        r'\(([A-Za-z]\d+[a-z]?)[-)]',            # "(A9a-" or "(A9a)"
        r'\b([A-Za-z]\d+[a-z]?)-[A-Za-z0-9]',   # "A9a-BL01"
        r'[-_]([A-Za-z]\d+[a-z]?)[-_\.]',        # "-A9a-"
        r'([A-Za-z]\d+[a-z]?)\.dwg',             # "A9a.dwg"
        r'\b([A-Za-z]\d+[a-z]?)\b',              # standalone "A9a"
        r'^([A-Za-z]\d+[a-z]?)\s',                # "A9 " at start of filename
    ]
    for p in patterns:
        m = re.search(p, fname)
        if m:
            candidate = m.group(1)
            # Plot-like: single letter + 1-2 digits + optional a/b suffix
            if re.match(r'^[A-Za-z]\d{1,2}[a-z]?$', candidate):
                # Uppercase letter+digits, preserve lowercase suffix
                return re.sub(
                    r'^([A-Za-z])(\d+)([a-z]?)$',
                    lambda x: x.group(1).upper() + x.group(2) + x.group(3).lower(),
                    candidate
                )
    return None


class PlotRegistry:
    def __init__(self):
        self._cache = {}

    def get(self, dwg_path):
        if dwg_path not in self._cache:
            plot = detect_plot_from_path(dwg_path)
            self._cache[dwg_path] = plot
            if plot:
                Logger.ok(f"Detected plot '{plot}' from {os.path.basename(dwg_path)}")
            else:
                Logger.warn(f"Could not detect plot from {os.path.basename(dwg_path)}")
        return self._cache[dwg_path]


# ============================================================
# BLOCK_NO FROM LAYER NAME
# "Equipment Block-07"  → "ZONE_BLK07"
# "Equipment Block-12"  → "ZONE_BLK12"
# "Equipment-03"        → "ZONE_BLK03"
# ============================================================

def block_no_from_layer(layer_name, plot_id):
    """Extract block number from layer name suffix and format as A5_BLK07."""
    if not layer_name:
        return None
    nums = re.findall(r'\d+', layer_name)
    if not nums:
        return None
    blk_num = int(nums[-1])
    return f"{plot_id}_BLK{blk_num:02d}"


# ============================================================
# BLOCK_NO FORMATTER (from MTEXT raw value)
# ============================================================

def format_block_no(raw_val, plot_id):
    """Reformat DWG MTEXT block label. Suffix always lowercase.
    'A9a- BL-40' -> 'A9a_BLK40'
    'A9B- BL-07' -> 'A9b_BLK07'
    'A9A- BL-01' -> 'A9a_BLK01'
    'A9- BL-05'  -> 'A9_BLK05'
    """
    if not raw_val:
        return raw_val
    raw_clean  = raw_val.strip()
    # Capture letter + digits + optional suffix (upper or lower)
    plot_match = re.match(r'([A-Za-z]\d+[A-Za-z]?)', raw_clean)
    if plot_match:
        raw_detected = plot_match.group(1)
        detected = re.sub(
            r'^([A-Za-z])(\d+)([A-Za-z]?)$',
            lambda m: m.group(1).upper() + m.group(2) + m.group(3).lower(),
            raw_detected
        )
    else:
        detected = plot_id
    nums = re.findall(r'\d+', raw_clean)
    if not nums:
        return raw_clean
    return f"{detected}_BLK{int(nums[-1]):02d}"


def detect_plot_from_path(path):
    """Extract plot ID from DWG filename eg 'P01' from 'Layout (Plot P01)'"""
    fname    = os.path.basename(path)
    patterns = [
        r'[Pp]lot\s+([A-Za-z]\d+[a-z]?)',      # "Plot A9a"
        r'[Rr]outing\s+([A-Za-z]\d+[a-z]?)',    # "Routing A9a"
        r'[Bb]oundary([A-Za-z]\d+[a-z]?)',        # "BlockBoundaryA9a"
        r'[Bb]lock[A-Za-z]*([A-Za-z]\d+[a-z]?)', # "BlockA9a"
        r'\(([A-Za-z]\d+[a-z]?)[-)]',            # "(A9a-" or "(A9a)"
        r'\b([A-Za-z]\d+[a-z]?)-[A-Za-z0-9]',   # "A9a-BL01"
        r'[-_]([A-Za-z]\d+[a-z]?)[-_\.]',        # "-A9a-"
        r'([A-Za-z]\d+[a-z]?)\.dwg',             # "A9a.dwg"
        r'\b([A-Za-z]\d+[a-z]?)\b',              # standalone "A9a"
        r'^([A-Za-z]\d+[a-z]?)\s',                # "A9 " at start of filename
    ]
    for p in patterns:
        m = re.search(p, fname)
        if m:
            candidate = m.group(1)
            # Plot-like: single letter + 1-2 digits + optional a/b suffix
            if re.match(r'^[A-Za-z]\d{1,2}[a-z]?$', candidate):
                # Uppercase letter+digits, preserve lowercase suffix
                return re.sub(
                    r'^([A-Za-z])(\d+)([a-z]?)$',
                    lambda x: x.group(1).upper() + x.group(2) + x.group(3).lower(),
                    candidate
                )
    return None


class AutoCADManager:

    def __init__(self):
        self.acad = None
        self.docs = {}
        self._connect()

    def _connect(self):
        if not HAS_WIN32:
            Logger.err("pywin32 not installed. Run: pip install pywin32")
            sys.exit(1)
        try:
            self.acad = win32com.client.Dispatch("AutoCAD.Application")
            Logger.ok(f"Connected to AutoCAD {self.acad.Version}")
        except Exception as e:
            Logger.err(f"Cannot connect to AutoCAD: {e}")
            Logger.err("Make sure AutoCAD is open.")
            sys.exit(1)

    def _get_doc_by_path(self, path):
        norm        = os.path.normpath(path).lower()
        target_base = os.path.basename(norm)
        try:
            for doc in self.acad.Documents:
                try:
                    full = str(doc.FullName or "").strip()
                    if full:
                        doc_path = os.path.normpath(full).lower()
                        if doc_path == norm:
                            return doc
                        if os.path.basename(doc_path) == target_base:
                            Logger.warn(f"Matched by filename: {doc.Name}")
                            return doc
                    else:
                        # FullName blank — open but unsaved/read-only; match on doc.Name
                        doc_name = str(doc.Name or "").strip().lower()
                        doc_stem = os.path.splitext(doc_name)[0]
                        tgt_stem = os.path.splitext(target_base)[0]
                        if doc_name == target_base or doc_stem == tgt_stem:
                            Logger.warn(f"Matched by Name (no FullName): {doc.Name}")
                            return doc
                except Exception:
                    continue
        except Exception:
            try:
                doc = self.acad.ActiveDocument
                if doc:
                    full = str(doc.FullName or "").strip()
                    match_base = (os.path.basename(os.path.normpath(full).lower())
                                  if full else str(doc.Name or "").strip().lower())
                    if match_base == target_base:
                        return doc
            except Exception:
                pass
        return None

    def get_doc(self, path):
        if path in self.docs:
            return self.docs[path]
        doc = self._get_doc_by_path(path)
        if not doc:
            Logger.err(f"DWG not open in AutoCAD: {os.path.basename(path)}")
            return None
        self.docs[path] = doc
        Logger.ok(f"Found open DWG: {doc.Name}")
        return doc

    def get_modelspace(self, path):
        doc = self.get_doc(path)
        return doc.ModelSpace if doc else None

    def list_open_docs(self):
        names = []
        try:
            for doc in self.acad.Documents:
                try:
                    names.append(doc.FullName)
                except Exception:
                    pass
        except Exception:
            try:
                names.append(self.acad.ActiveDocument.FullName)
            except Exception:
                pass
        return names


# ============================================================
# ENTITY READER
# ============================================================


class EntityReader:

    @staticmethod
    def etype(entity):
        try:
            name = entity.EntityName.upper()
            return {
                "ACDBBLOCKREFERENCE": "INSERT",
                "ACDBPOLYLINE":       "LWPOLYLINE",
                "ACDB2DPOLYLINE":     "POLYLINE",
                "ACDBLINE":           "LINE",
                "ACDBPOINT":          "POINT",
                "ACDBMTEXT":          "MTEXT",
                "ACDBTEXT":           "TEXT",
                "ACDBCIRCLE":         "CIRCLE",
                "ACDBARC":            "ARC",
            }.get(name, name)
        except Exception:
            return ""

    @staticmethod
    def block_fixed_coords(ent, local_pts, forced_rotation_deg=None):
        """Transform hardcoded block-local polygon vertices to world coordinates.

        Uses INSERT insertion point and scale. For rotation:
        - If forced_rotation_deg is set → use that angle exactly
        - Otherwise → use INSERT's actual Rotation property

        The block-local vertices are in the block editor's coordinate system
        (landscape). The forced rotation corrects the orientation to match
        how the geometry appears in the world — same logic as block_explode_enclose
        which always forces 90° because the block editor is landscape.
        """
        try:
            ix, iy = EntityReader.insert_point(ent)
            sx     = getattr(ent, 'XScaleFactor', 1.0)
            sy     = getattr(ent, 'YScaleFactor', 1.0)

            if forced_rotation_deg is not None:
                rot = math.radians(float(forced_rotation_deg))
            else:
                try:
                    rot = math.radians(ent.Rotation)
                except Exception:
                    rot = 0.0

            cos_r, sin_r = math.cos(rot), math.sin(rot)
            result = []
            for lx, ly in local_pts:
                wx = ix + (lx * sx * cos_r - ly * sy * sin_r)
                wy = iy + (lx * sx * sin_r + ly * sy * cos_r)
                result.append((wx, wy))

            if result and result[0] != result[-1]:
                result.append(result[0])
            return result
        except Exception:
            return None
        if not text: return ''
        import re as _re
        text = _re.sub(r'[{]\\f[^;]*;([^}]*)[}]', r'\1', text)
        text = _re.sub(r'[{][^}]*[}]', '', text)
        text = _re.sub(r'\\[a-zA-Z]+[0-9]*', '', text)
        return text.strip()
    @staticmethod
    def lwpoly_to_polygon(pts, half_width):
        """Buffer LWPOLYLINE centerline to closed polygon."""
        if not pts or len(pts) < 2: return None
        def perp(dx, dy, hw):
            ln = math.sqrt(dx*dx + dy*dy)
            if ln < 1e-10: return 0.0, 0.0
            return -dy/ln*hw, dx/ln*hw
        n = len(pts)
        left, right = [], []
        for i in range(n):
            dx,dy = (pts[i+1][0]-pts[i][0], pts[i+1][1]-pts[i][1]) if i<n-1 else (pts[i][0]-pts[i-1][0], pts[i][1]-pts[i-1][1])
            ox,oy = perp(dx, dy, half_width)
            left.append((pts[i][0]+ox, pts[i][1]+oy))
            right.append((pts[i][0]-ox, pts[i][1]-oy))
        poly = left + list(reversed(right))
        poly.append(poly[0])
        return poly

    @staticmethod
    def dist_pt_seg(px, py, ax, ay, bx, by):
        dx,dy = bx-ax, by-ay
        if dx==0 and dy==0: return math.sqrt((px-ax)**2+(py-ay)**2), 0.0
        t = max(0.0, min(1.0, ((px-ax)*dx+(py-ay)*dy)/(dx*dx+dy*dy)))
        return math.sqrt((px-(ax+t*dx))**2+(py-(ay+t*dy))**2), t

    @staticmethod
    def split_by_section_marks(pts, section_marks, threshold=50):
        """Split polyline at section mark positions, label each segment."""
        def snap(px, py):
            best = None
            for i in range(len(pts)-1):
                d,t = EntityReader.dist_pt_seg(px,py,pts[i][0],pts[i][1],pts[i+1][0],pts[i+1][1])
                if d<=threshold and (best is None or d<best[2]): best=(i,t,d)
            return best
        seen = {}
        for sm in section_marks:
            s = snap(sm['pos'][0], sm['pos'][1])
            if s:
                key = f"{s[0]}_{s[1]:.3f}"
                if key not in seen or s[2]<seen[key][2]: seen[key]=(s[0],s[1],s[2],sm['label'])
        snaps = sorted(seen.values(), key=lambda x:(x[0],x[1]))
        deduped = []
        for si,t,d,label in snaps:
            if deduped and abs((si+t)-(deduped[-1][0]+deduped[-1][1]))<0.05:
                if d<deduped[-1][2]: deduped[-1]=(si,t,d,label)
            else: deduped.append((si,t,d,label))
        def interp(i,t):
            ax,ay=pts[i]; bx,by=pts[i+1]
            return (ax+t*(bx-ax), ay+t*(by-ay))
        cuts = [(0,0.0,'')] + [(s[0],s[1],s[3]) for s in deduped]
        segs = []
        for k in range(len(cuts)):
            ci,ct,label = cuts[k]
            ni,nt = (cuts[k+1][0],cuts[k+1][1]) if k+1<len(cuts) else (len(pts)-2,1.0)
            sub = [interp(ci,ct) if ct<1.0 else pts[ci+1]]
            sv = ci+1 if ct<1.0 else ci+2
            ev = ni+1 if nt>0.0 else ni
            for v in range(sv, min(ev+1,len(pts))): sub.append(pts[v])
            if 0.0<nt<1.0:
                ep=interp(ni,nt)
                if sub[-1]!=ep: sub.append(ep)
            if len(sub)>=2:
                ln=sum(math.sqrt((sub[j+1][0]-sub[j][0])**2+(sub[j+1][1]-sub[j][1])**2) for j in range(len(sub)-1))
                if ln>0.1: segs.append({'pts':sub,'label':label})
        return segs

    @staticmethod
    def lwpoly_coords(ent, force_close=True):
        """Read LWPOLYLINE coordinates with bulge arc interpolation.

        force_close=True  → polygon geometry (always close the ring)
        force_close=False → line geometry   (only close if entity.Closed is True)

        Bulge segments are expanded to 8 intermediate arc points so rounded
        corners (e.g. cable routes with r=0.9m corners) render correctly.
        When all bulges are zero (rectilinear geometry) the arc path is
        skipped entirely — the common case for rectilinear polygon layers.
        """
        try:
            cr   = list(ent.Coordinates)
            pts  = [(cr[i], cr[i+1]) for i in range(0, len(cr), 2)]
            if not pts:
                return None

            # Check if entity is truly closed
            try:
                is_closed = bool(ent.Closed)
            except Exception:
                is_closed = False

            # Fast path: no bulges — skip arc interpolation entirely
            try:
                bulges = list(ent.GetBulges())
            except Exception:
                bulges = []

            if not bulges or not any(abs(b) > 1e-9 for b in bulges):
                # No arcs — return raw vertex list directly
                if is_closed:
                    return pts + [pts[0]]
                result = pts[:]
                if force_close and result[0] != result[-1]:
                    result.append(result[0])
                return result

            # Pad bulges list to match vertex count
            while len(bulges) < len(pts):
                bulges.append(0.0)

            # Build expanded point list with arc interpolation for bulge segments
            ARC_STEPS = 8   # intermediate points per bulge arc
            result = []
            n = len(pts)
            segments = n if is_closed else n - 1

            for i in range(segments):
                p1 = pts[i]
                p2 = pts[(i + 1) % n]
                b  = bulges[i] if i < len(bulges) else 0.0
                result.append(p1)

                if abs(b) > 1e-9:
                    x1, y1 = p1
                    x2, y2 = p2
                    chord = math.sqrt((x2-x1)**2 + (y2-y1)**2)
                    if chord > 1e-10:
                        sagitta   = abs(b) * chord / 2.0
                        radius    = (chord**2 / (8 * sagitta) + sagitta / 2.0
                                     if abs(sagitta) > 1e-12 else chord / 2.0)
                        mx, my    = (x1+x2)/2.0, (y1+y2)/2.0
                        dx, dy    = x2-x1, y2-y1
                        perp_len  = math.sqrt(dx*dx + dy*dy)
                        ux, uy    = -dy/perp_len, dx/perp_len
                        d_to_ctr  = math.sqrt(max(0.0, radius**2 - (chord/2.0)**2))
                        sign      = -1.0 if b > 0 else 1.0
                        cx        = mx + sign * ux * d_to_ctr
                        cy        = my + sign * uy * d_to_ctr
                        a1 = math.atan2(y1-cy, x1-cx)
                        a2 = math.atan2(y2-cy, x2-cx)
                        if b > 0:
                            if a2 < a1: a2 += 2*math.pi
                        else:
                            if a2 > a1: a2 -= 2*math.pi
                        for step in range(1, ARC_STEPS):
                            t  = step / ARC_STEPS
                            a  = a1 + t * (a2 - a1)
                            result.append((cx + radius*math.cos(a),
                                           cy + radius*math.sin(a)))

            if is_closed:
                result.append(result[0])
            else:
                result.append(pts[-1])
                if force_close and result[0] != result[-1]:
                    result.append(result[0])

            return result
        except Exception:
            return None

    @staticmethod
    def poly_coords(ent):
        try:
            pts = []
            for v in ent.vertices:
                c = v.dxf.location
                pts.append((c.x, c.y))
            if pts and pts[0] != pts[-1]:
                pts.append(pts[0])
            return pts
        except Exception:
            return None

    @staticmethod
    def line_coords(ent):
        try:
            sp = ent.StartPoint
            ep = ent.EndPoint
            return [(sp[0], sp[1]), (ep[0], ep[1])]
        except Exception:
            return None

    @staticmethod
    def point_coords(ent):
        try:
            c = ent.InsertionPoint
            return [(c[0], c[1])]
        except Exception:
            try:
                c = ent.Coordinates
                return [(c[0], c[1])]
            except Exception:
                return None

    @staticmethod
    def block_attrs(ent):
        attrs = {}
        try:
            if ent.HasAttributes:
                for att in ent.GetAttributes():
                    tag = att.TagString.upper().strip()
                    val = att.TextString.strip() if att.TextString else ""
                    attrs[tag] = val
        except Exception:
            pass
        return attrs

    @staticmethod
    def block_attr_positions(ent):
        """Return {TAG: (world_x, world_y)} for every attribute on an INSERT.

        Each attribute stores its own InsertionPoint in world coordinates
        (AutoCAD already applies the block transform when you read att.InsertionPoint
        on an attribute reference obtained via GetAttributes()).
        Falls back to the INSERT insertion point if reading fails.
        """
        positions = {}
        fallback  = EntityReader.insert_point(ent)
        try:
            if ent.HasAttributes:
                for att in ent.GetAttributes():
                    tag = att.TagString.upper().strip()
                    try:
                        ip = att.InsertionPoint
                        positions[tag] = (ip[0], ip[1])
                    except Exception:
                        positions[tag] = fallback
        except Exception:
            pass
        return positions

    @staticmethod
    def block_text(ent):
        attrs = EntityReader.block_attrs(ent)
        return list(attrs.values())[0] if attrs else ""

    @staticmethod
    def mtext_content(ent):
        try:
            raw   = ent.TextString
            clean = re.sub(r'\\[A-Za-z][^;]*;', '', raw)
            clean = re.sub(r'[{}]', '', clean)
            return clean.replace("\\P", " ").strip()
        except Exception:
            return ""

    @staticmethod
    def insert_point(ent):
        try:
            c = ent.InsertionPoint
            return (c[0], c[1])
        except Exception:
            return (0.0, 0.0)

    @staticmethod
    def block_def_coords(doc, block_name, ent):
        """Read polygon from block definition.
        Picks polyline closest to _target_area hint (set by caller).
        Falls back to largest polyline if no hint.
        Caches per (doc, block_name, target_area) for speed.
        """
        try:
            ix, iy = EntityReader.insert_point(ent)
            sx = getattr(ent, 'XScaleFactor', 1.0)
            sy = getattr(ent, 'YScaleFactor', 1.0)
            try:
                if getattr(EntityReader, '_geom_type_hint', '') == 'block_explode_enclose':
                    # Block definition is landscape in block editor coords.
                    # Use exactly 90° — ignore INSERT rotation (causes tilt).
                    rot = math.radians(90.0)
                else:
                    rot = math.radians(ent.Rotation)
            except Exception:
                rot = 0.0

            if not hasattr(EntityReader, '_blk_cache'):
                EntityReader._blk_cache = {}
            target_hint = getattr(EntityReader, '_target_area', 0)
            cache_key   = (id(doc), block_name, round(target_hint, 4))


            if cache_key not in EntityReader._blk_cache:
                # Retry up to 3 times — AutoCAD sometimes rejects COM calls
                # with RPC_E_CALL_REJECTED (0x80010001) when busy or when
                # the block editor is open.  A short sleep usually resolves it.
                _blk_def   = None
                _last_err  = None
                for _retry in range(3):
                    try:
                        _blk_def = doc.Blocks.Item(block_name)
                        break
                    except Exception as _e:
                        _last_err = _e
                        time.sleep(0.2 * (_retry + 1))
                if _blk_def is None:
                    Logger.warn(f"block_def_coords: cannot open block '{block_name}' "
                                f"after 3 retries: {_last_err}")
                    EntityReader._blk_cache[cache_key] = None
                    return None
                blk_def   = _blk_def
                all_polys = []

                def _scan_block_def(bdef, depth=0):
                    """Recursively scan a block definition for LWPOLYLINE/POLYLINE,
                    following nested INSERT references up to 3 levels deep."""
                    if depth > 3:
                        return
                    for sub in bdef:
                        try:
                            sn = sub.EntityName.upper()
                        except Exception:
                            continue
                        stype = {"ACDBPOLYLINE": "LWPOLYLINE",
                                 "ACDB2DPOLYLINE": "POLYLINE"}.get(sn, "")
                        if stype == "LWPOLYLINE":
                            try:
                                cr   = list(sub.Coordinates)
                                cpts = [(cr[j], cr[j+1]) for j in range(0, len(cr), 2)]
                                if cpts and len(cpts) >= 2:
                                    if cpts[0] != cpts[-1]:
                                        cpts = cpts + [cpts[0]]
                                    a = abs(sum(
                                        cpts[k][0]*cpts[k+1][1] - cpts[k+1][0]*cpts[k][1]
                                        for k in range(len(cpts)-1)
                                    ) / 2.0)
                                    all_polys.append((a, cpts))
                            except Exception:
                                pass
                        elif stype == "POLYLINE":
                            try:
                                cpts = []
                                for v in sub.vertices:
                                    c = v.dxf.location
                                    cpts.append((c.x, c.y))
                                if cpts and len(cpts) >= 2:
                                    if cpts[0] != cpts[-1]:
                                        cpts = cpts + [cpts[0]]
                                    a = abs(sum(
                                        cpts[k][0]*cpts[k+1][1] - cpts[k+1][0]*cpts[k][1]
                                        for k in range(len(cpts)-1)
                                    ) / 2.0)
                                    all_polys.append((a, cpts))
                            except Exception:
                                pass
                        elif sn == "ACDBBLOCKREFERENCE" and depth < 3:
                            # Nested INSERT — recurse into its block definition
                            try:
                                nested_name = sub.Name
                                nested_def  = doc.Blocks.Item(nested_name)
                                _scan_block_def(nested_def, depth + 1)
                            except Exception:
                                pass

                _scan_block_def(blk_def)

                if not all_polys:
                    EntityReader._blk_cache[cache_key] = None
                elif target_hint > 0:
                    # Always log ALL polygon areas so target_area_sqm can be diagnosed.
                    # Shows raw block-local areas AND the /1e4 /1e6 scaled variants
                    # so the correct target value is immediately visible in the log.
                    _sorted_areas = sorted([round(a, 6) for a, _ in all_polys], reverse=True)
                    _scaled = [(round(a,4), round(a/1e4,6), round(a/1e6,8))
                               for a in _sorted_areas]
                    Logger.info(f"    block '{block_name}' polygon areas "
                                f"(raw | /1e4 | /1e6): {_scaled}")
                    best_pts  = None
                    best_diff = float('inf')
                    for a, p in all_polys:
                        for divisor in (1, 1e4, 1e6):
                            diff = abs((a / divisor) - target_hint)
                            if diff < best_diff:
                                best_diff = diff
                                best_pts  = p
                    Logger.info(f"    best match diff={round(best_diff,4)} "
                                f"(tolerance={target_hint})")
                    if best_diff > target_hint:
                        Logger.warn(f"    NO polygon within tolerance {target_hint} "
                                    f"of target {target_hint} — set target_area_sqm "
                                    f"to one of the raw values above or a /1e4 /1e6 scaled value")
                    EntityReader._blk_cache[cache_key] = best_pts
                else:
                    all_polys.sort(key=lambda x: x[0], reverse=True)
                    EntityReader._blk_cache[cache_key] = all_polys[0][1]

            raw_pts = EntityReader._blk_cache[cache_key]
            if not raw_pts:
                return None


            cos_r = math.cos(rot)
            sin_r = math.sin(rot)
            world = []
            for (bx, by) in raw_pts:
                wx = ix + (bx * sx * cos_r - by * sy * sin_r)
                wy = iy + (bx * sx * sin_r + by * sy * cos_r)
                world.append((wx, wy))
            if world and world[0] != world[-1]:
                world.append(world[0])
            if len(world) < 4:
                return None
            EntityReader._blk_cache[cache_key] = raw_pts
            return world

        except Exception as e:
            Logger.warn("block_def_coords error: " + str(e))
            return None


# Module-level singleton — EntityReader has no instance state;
# re-instantiating it per matched layer was pure overhead.
_er = EntityReader()


# ============================================================
# LAYER FINDER — keyword-based intelligent matching
# ============================================================

# ── MSP INDEX: built once per msp, reused for all layer lookups ──────────────
_msp_index_cache = {}   # id(msp) -> {layer_name: [entities], '__types__': {layer: {type:n}}}

def _build_msp_index(msp):
    """Single COM scan: builds layer→entities dict AND type counts."""
    idx   = {}   # layer -> [ent, ...]
    types = {}   # layer -> {etype: count}
    # Local reference to the entity-name map — avoids repeated global lookups
    _emap = {
        "ACDBBLOCKREFERENCE": "INSERT",
        "ACDBPOLYLINE":       "LWPOLYLINE",
        "ACDB2DPOLYLINE":     "POLYLINE",
        "ACDBLINE":           "LINE",
        "ACDBPOINT":          "POINT",
        "ACDBMTEXT":          "MTEXT",
        "ACDBTEXT":           "TEXT",
        "ACDBCIRCLE":         "CIRCLE",
        "ACDBARC":            "ARC",
    }
    try:
        for ent in msp:
            try:
                lyr = ent.Layer
                et  = _emap.get(ent.EntityName.upper(), ent.EntityName.upper())
                if lyr not in idx:
                    idx[lyr]   = []
                    types[lyr] = {}
                idx[lyr].append((et, ent))
                lyr_types = types[lyr]
                lyr_types[et] = lyr_types.get(et, 0) + 1
            except Exception:
                continue
    except Exception:
        pass
    idx['__types__'] = types
    return idx

def get_msp_index(msp):
    key = id(msp)
    if key not in _msp_index_cache:
        _msp_index_cache[key] = _build_msp_index(msp)
    return _msp_index_cache[key]

def get_all_layers(msp):
    idx = get_msp_index(msp)
    return [k for k in idx.keys() if k != '__types__']


def find_layers(msp, source_layer, fallbacks=None, match_mode="exact"):
    """
    Returns list of matched layer names.
    match_mode='exact'    → returns [one layer]
    match_mode='prefix'   → returns [all layers starting with source_layer]
    match_mode='contains' → returns [all layers containing source_layer substring]

    Special case: if DWG has only layer '0', entities may still be usable
    — returns ['0'] and lets caller decide.
    """
    if not source_layer:
        return []

    all_layers = get_all_layers(msp)

    # Special case — DWG only has layer '0'
    # This happens with isolated/exported DWGs where layer info is stripped
    if all_layers == ['0'] or all_layers == []:
        Logger.warn(f"DWG has only layer '0' — using all entities regardless of layer")
        return ['__ALL__']

    # CONTAINS MODE
    if match_mode == "contains":
        needle = source_layer.lower()
        matched = [l for l in all_layers if needle in l.lower()]
        if matched:
            Logger.ok(f"Contains match '{source_layer}' → {len(matched)} layers")
            return sorted(matched)
        if fallbacks:
            for fb in fallbacks:
                matched = [l for l in all_layers if fb.lower() in l.lower()]
                if matched:
                    Logger.warn(f"Fallback contains '{fb}' → {len(matched)} layers")
                    return sorted(matched)
        Logger.err(f"No layers found containing '{source_layer}'")
        Logger.info(f"Available ({len(all_layers)}): {sorted(all_layers)}")
        return []

    # PREFIX MODE
    if match_mode == "prefix":
        prefix_lower = source_layer.lower()
        matched = [l for l in all_layers
                   if l.lower().startswith(prefix_lower)]
        if matched:
            Logger.ok(f"Prefix match '{source_layer}' → {len(matched)} layers")
            return sorted(matched)
        if fallbacks:
            for fb in fallbacks:
                matched = [l for l in all_layers
                           if l.lower().startswith(fb.lower())]
                if matched:
                    Logger.warn(f"Fallback prefix '{fb}' → {len(matched)} layers")
                    return sorted(matched)
        Logger.err(f"No layers found with prefix '{source_layer}'")
        Logger.info(f"Available ({len(all_layers)}): {sorted(all_layers)}")
        return []

    # EXACT MODE
    all_lower = {l.lower(): l for l in all_layers}

    if source_layer in all_layers:
        Logger.ok(f"Exact match: '{source_layer}'")
        return [source_layer]

    if source_layer.lower() in all_lower:
        m = all_lower[source_layer.lower()]
        Logger.warn(f"Case-insensitive match: '{m}'")
        return [m]

    auto_kws = [p for p in re.split(r'[\s\-_]+', source_layer)
                if len(p) > 2 and not p.replace('.','').isdigit()]
    if auto_kws:
        candidates = [(sum(1 for kw in auto_kws
                          if kw.lower() in l.lower()), l)
                      for l in all_layers]
        candidates = [(s, l) for s, l in candidates if s > 0]
        if candidates:
            best = max(candidates, key=lambda x: x[0])[1]
            Logger.warn(f"Keyword match: '{best}' for '{source_layer}'")
            return [best]

    if fallbacks:
        for fb in fallbacks:
            if fb in all_layers:
                Logger.warn(f"Fallback: '{fb}'")
                return [fb]
            if fb.lower() in all_lower:
                Logger.warn(f"Fallback case-insensitive: '{all_lower[fb.lower()]}'")
                return [all_lower[fb.lower()]]

    matches = difflib.get_close_matches(source_layer, all_layers, n=1, cutoff=0.6)
    if matches:
        Logger.warn(f"Fuzzy match: '{matches[0]}'")
        return [matches[0]]

    Logger.err(f"No match for '{source_layer}'")
    Logger.info(f"Available ({len(all_layers)}): {sorted(all_layers)}")
    return []


def get_entities_on_layer(msp, layer_name):
    """Get entities on a layer using pre-built index. No extra COM scan."""
    idx = get_msp_index(msp)
    if layer_name == '__ALL__':
        result = []
        for k, v in idx.items():
            if k != '__types__':
                result.extend(v)
        return result
    return idx.get(layer_name, [])


# ============================================================
# GEOMETRY HELPERS
# ============================================================

def poly_area(pts):
    n = len(pts)
    if n < 3:
        return 0.0
    a = 0.0
    for i in range(n - 1):
        a += pts[i][0] * pts[i+1][1] - pts[i+1][0] * pts[i][1]
    return abs(a) / 2.0


def poly_perimeter(pts):
    return sum(math.sqrt((pts[i+1][0]-pts[i][0])**2 +
                         (pts[i+1][1]-pts[i][1])**2)
               for i in range(len(pts)-1))


def line_len(pts):
    return poly_perimeter(pts)


def centroid(pts):
    if not pts:
        return (0.0, 0.0)
    return (sum(p[0] for p in pts)/len(pts),
            sum(p[1] for p in pts)/len(pts))


def dist2d(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2)


def _sq_dist(a, b):
    """Squared Euclidean distance — no sqrt, safe for nearest-neighbor comparisons."""
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    return dx*dx + dy*dy


def point_in_polygon(px, py, polygon):
    """Ray-casting point-in-polygon containment test.

    Works with both tuples (x, y) and lists [x, y] as polygon vertices.
    The ring may be open or closed (repeated first/last vertex is handled).
    Returns True when (px, py) lies inside the polygon, False otherwise.
    Edge/vertex coincidence is treated as inside (consistent behaviour).
    """
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > py) != (yj > py)):
            denom = yj - yi
            if abs(denom) > 1e-12:
                x_cross = (xj - xi) * (py - yi) / denom + xi
                if px < x_cross:
                    inside = not inside
        j = i
    return inside


# ============================================================
# SPATIAL JOIN ENGINE
# ============================================================

class SpatialJoinEngine:
    def __init__(self):
        self.cache           = {}
        self._plot_buckets   = {}   # layer → {plot_prefix: [features]} for O(1) filter
        self.spatial_ref_key = None  # name of the spatial reference layer in cache

    def register(self, name, features):
        # ── Pre-compute bounding boxes for polygon features ──────────────────
        # Stored as (xmin, xmax, ymin, ymax) on each feature dict so that
        # nearest_in_plot() and plot_from_position() can reject non-containing
        # polygons with 4 cheap comparisons before running the full PIP test.
        for f in features:
            poly = f.get("polygon")
            if poly and len(poly) >= 3 and "_bbox" not in f:
                xs = [v[0] for v in poly]
                ys = [v[1] for v in poly]
                f["_bbox"] = (min(xs), max(xs), min(ys), max(ys))

        self.cache[name] = features

        # ── Pre-bucket by plot prefix ────────────────────────────────────────
        # Converts the per-call list-comprehension filter in nearest_in_plot()
        # into a single O(1) dict lookup.  Built for any layer whose features
        # carry a "Block_No"-style field (e.g. 'A9a_BLK01').
        buckets = {}
        for f in features:
            props = f.get("properties", {})
            for fkey in ("Block_No", "block_no", "BLOCK_NO"):
                bn = str(props.get(fkey) or "").strip()
                if "_BLK" in bn:
                    pfx = bn.split("_BLK")[0]
                    buckets.setdefault(pfx, []).append(f)
                    break
        if buckets:
            self._plot_buckets[name] = buckets

        Logger.ok(f"Registered {len(features)} features for join: '{name}'")

    def nearest(self, point, layer_name, field):
        feats = self.cache.get(layer_name)
        if not feats:
            Logger.warn(f"Spatial join: '{layer_name}' not cached")
            return None
        best_val, best_d2 = None, float("inf")
        for f in feats:
            c = f.get("centroid")
            if not c:
                continue
            d2 = _sq_dist(point, c)
            if d2 < best_d2:
                best_d2 = d2
                props  = f.get("properties", {})
                val = props.get(field)
                if val is None:
                    for k, v in props.items():
                        if k.lower() == field.lower():
                            val = v
                            break
                best_val = val
        return best_val

    def plot_from_position(self, point):
        """Determine zone ID by point-in-polygon test against the spatial reference layer.

        Uses bbox pre-filter + ray-cast PIP for exact containment — faster
        and more accurate than the previous nearest-centroid approach.
        Falls back to nearest centroid only when the point lies outside all
        reference polygons (e.g. a feature in a road-gap or DWG corner artefact).
        Returns None when the spatial reference layer is not yet in the cache.
        """
        feats = self.cache.get(self.spatial_ref_key) if self.spatial_ref_key else None
        if not feats:
            return None
        px, py = point[0], point[1]

        # PIP with bbox pre-filter — typically resolves in 1-2 iterations
        for f in feats:
            bbox = f.get("_bbox")
            if bbox and not (bbox[0] <= px <= bbox[1] and bbox[2] <= py <= bbox[3]):
                continue
            poly = f.get("polygon")
            if poly and len(poly) >= 3 and point_in_polygon(px, py, poly):
                block_no = str(f.get("properties", {}).get("Block_No", "")).strip()
                if "_BLK" in block_no:
                    return block_no.split("_BLK")[0]
                elif block_no and block_no not in (" ", "null", "None", ""):
                    m = _RE_PLOT_PREFIX.match(block_no)
                    return m.group(1) if m else None

        # Fallback: nearest centroid (point outside all block polygons)
        best_plot, best_d = None, float("inf")
        for f in feats:
            c = f.get("centroid")
            if not c:
                continue
            d = dist2d(point, c)
            if d < best_d:
                best_d = d
                block_no = str(f.get("properties", {}).get("Block_No", "")).strip()
                if "_BLK" in block_no:
                    best_plot = block_no.split("_BLK")[0]
                elif block_no and block_no not in (" ", "null", "None", ""):
                    m = _RE_PLOT_PREFIX.match(block_no)
                    best_plot = m.group(1) if m else None
        return best_plot

    def nearest_in_plot(self, point, layer_name, field, plot_id=None):
        """Plot-aware, polygon-aware Block_No lookup.

        Resolution order:
          1. PIP + bbox pre-filter — plot-filtered (pre-bucketed, O(1) lookup)
          2. PIP + bbox pre-filter — unfiltered (safety net)
          3. Nearest centroid      — plot-filtered
          4. Nearest centroid      — unfiltered (last resort)

        Performance:
        - Filtered candidate list comes from a pre-bucketed index built at
          register() time — no per-call list comprehension.
        - Bbox pre-filter rejects non-containing polygons with 4 comparisons
          before the full PIP ray-cast, typically reducing PIP tests to 1.
        - PIP returns on first containment hit (blocks don't overlap).
        """
        feats = self.cache.get(layer_name)
        if not feats:
            Logger.warn(f"Spatial join: '{layer_name}' not cached")
            return None

        px, py    = point[0], point[1]
        _fl_lower = field.lower()

        def _fv(f):
            """Extract field value — direct get first, case-insensitive fallback."""
            fp = f.get("properties", {})
            v  = fp.get(field)
            if v is None:
                for k, val in fp.items():
                    if k.lower() == _fl_lower:
                        return val
            return v

        # O(1) dict lookup replaces per-call list comprehension
        _pid = str(plot_id or "").strip()
        if _pid and _pid not in (" ", "A?", "") and _RE_HAS_SUBSUFFIX.search(_pid):
            filtered = self._plot_buckets.get(layer_name, {}).get(_pid, [])
        else:
            filtered = []

        # 1. PIP+bbox — plot-filtered
        if filtered:
            for f in filtered:
                bbox = f.get("_bbox")
                if bbox and not (bbox[0] <= px <= bbox[1] and bbox[2] <= py <= bbox[3]):
                    continue
                poly = f.get("polygon")
                if poly and len(poly) >= 3 and point_in_polygon(px, py, poly):
                    return _fv(f)

        # 2. PIP+bbox — unfiltered
        for f in feats:
            bbox = f.get("_bbox")
            if bbox and not (bbox[0] <= px <= bbox[1] and bbox[2] <= py <= bbox[3]):
                continue
            poly = f.get("polygon")
            if poly and len(poly) >= 3 and point_in_polygon(px, py, poly):
                return _fv(f)

        # 3. Nearest centroid — plot-filtered
        if filtered:
            best_val, best_d2 = None, float("inf")
            for f in filtered:
                c = f.get("centroid")
                if c:
                    d2 = _sq_dist(point, c)
                    if d2 < best_d2:
                        best_d2, best_val = d2, _fv(f)
            if best_val is not None:
                return best_val

        # 4. Nearest centroid — unfiltered (last resort)
        best_val, best_d2 = None, float("inf")
        for f in feats:
            c = f.get("centroid")
            if c:
                d2 = _sq_dist(point, c)
                if d2 < best_d2:
                    best_d2, best_val = d2, _fv(f)
        return best_val

    def assign_exclusive(self, features, layer_name, field,
                         pts_key="_pts", result_field="Start_Connection",
                         transform_fmt="", plot_field="Plot_No"):
        """
        One-to-one greedy assignment: each cached reference feature is assigned
        to exactly ONE source feature — the source whose nearest endpoint is closest.

        Uses insert_pt (block INSERT point) when available — this is the exact
        coordinate the line terminus snaps to in AutoCAD.  Falls back to
        centroid for features loaded from disk (no INSERT point stored).

        Algorithm:
          1. Build all (src_idx, endpoint_pt, ref_idx, ref_val, distance) tuples.
          2. Sort ascending by distance.
          3. Walk the list: assign if neither src_idx nor ref_idx already taken.
          4. Patch features[src_idx]["properties"][result_field] in-place.
        """
        target_feats = self.cache.get(layer_name)
        if not target_feats:
            Logger.warn(f"assign_exclusive: '{layer_name}' not in cache")
            return

        # Pre-extract target coordinates and field values once — avoids repeated
        # .get() + case-insensitive fallback loop inside the O(n*m) candidate build.
        _field_lower = field.lower()
        _target_pts = []
        for target in target_feats:
            c = target.get("insert_pt") or target.get("centroid")
            if not c:
                _target_pts.append(None)
                continue
            target_props = target.get("properties", {})
            val = target_props.get(field)
            if val is None:
                for k, v in target_props.items():
                    if k.lower() == _field_lower:
                        val = v
                        break
            _target_pts.append((c, val) if (val and val != " ") else None)

        # Build candidate list — use squared distance (no sqrt needed for ordering)
        candidates = []
        for ci, feat in enumerate(features):
            pts = feat.get(pts_key)
            if not pts or len(pts) < 2:
                continue
            for ep in (pts[0], pts[-1]):
                for ii, cv in enumerate(_target_pts):
                    if cv is None:
                        continue
                    c, val = cv
                    candidates.append((_sq_dist(ep, c), ci, ii, val, ep))

        candidates.sort(key=lambda x: x[0])

        assigned_sources  = set()
        assigned_targets  = set()
        assignment        = {}   # ci → formatted val

        for d, ci, ii, raw_val, ep in candidates:
            if ci in assigned_sources or ii in assigned_targets:
                continue
            assigned_sources.add(ci)
            assigned_targets.add(ii)

            # Apply transform if fmt provided
            if transform_fmt:
                feat_props = features[ci].get("properties", {})
                raw_str    = str(raw_val)
                # Strip leading alpha prefix for simple "PREFIX-digits" values.
                # Preserves compound values like "ZONE_BLK01" intact.
                m_strip = re.match(r'^[A-Za-z]+[-_]+0*(\d+)$', raw_str)
                digits  = m_strip.group(1) if m_strip else raw_str
                try:
                    subs = {k: (v or "") for k, v in feat_props.items()}
                    subs[field] = digits
                    result = transform_fmt.format_map(subs)
                    result = re.sub(r'([A-Za-z]\d+[a-z]?)_BLK', r'\1-BLK', result)
                    assignment[ci] = result
                except (KeyError, ValueError):
                    assignment[ci] = raw_val
            else:
                assignment[ci] = raw_val

        # Patch features in-place
        patched = 0
        for ci, val in assignment.items():
            features[ci]["properties"][result_field] = val
            patched += 1

        unmatched = len(features) - patched
        Logger.ok(f"assign_exclusive '{result_field}': {patched} matched, "
                  f"{unmatched} unmatched (no target in range)")
        if unmatched > 0:
            Logger.warn(f"  {unmatched} features got no {result_field} — "
                        f"check '{layer_name}' is in cache and DWGs overlap spatially")

    def nearest_endpoint(self, pts, layer_name, field, radius_m=None):
        """Return (start_val, end_val) for a line's start and end points.

        Finds the nearest cached feature to each endpoint with no radius
        cutoff — radius_m is accepted for API compatibility but ignored,
        because a line always connects to exactly one target feature and a hard
        radius causes silent misses when lines are long or target centroids
        are offset from the line terminus.
        """
        if not pts or len(pts) < 2:
            return None, None
        feats = self.cache.get(layer_name)
        if not feats:
            Logger.warn(f"nearest_endpoint: '{layer_name}' not in spatial cache")
            return None, None

        def _find_with_dist(point):
            best_val, best_d2 = None, float("inf")
            for f in feats:
                c = f.get("centroid")
                if not c:
                    continue
                d2 = _sq_dist(point, c)
                if d2 < best_d2:
                    best_d2 = d2
                    props = f.get("properties", {})
                    val = props.get(field)
                    if val is None:
                        for k, v in props.items():
                            if k.lower() == field.lower():
                                val = v
                                break
                    best_val = val
            return best_val, best_d2

        sv_val, sv_d2 = _find_with_dist(pts[0])
        ev_val, ev_d2 = _find_with_dist(pts[-1])
        # _sv = value from whichever line endpoint is closest to a cached target feature.
        # Handles cases where the matching end happens to be pts[-1] not pts[0].
        # _ev = the other endpoint's value (used by layers that need both ends).
        # Squared distances are compared — ordering is preserved without sqrt.
        if sv_d2 <= ev_d2:
            return sv_val, ev_val
        else:
            return ev_val, sv_val


# ============================================================
# FIELD ENGINE
# ============================================================

class FieldEngine:

    def __init__(self, global_cfg, plot_registry):
        self.global_cfg       = global_cfg
        self.plot_registry    = plot_registry
        self._current_dwg     = None
        self._current_layer   = None
        self._cached_plot     = "A?"   # cached result of get_plot()
        self._cached_plot_num = 0      # cached numeric part
        self._plot_log_seen   = set()
        self._miss_warned     = set()

    def set_dwg(self, path):
        self._current_dwg = path
        # Pre-compute plot and plot_num once per DWG — eliminates per-feature
        # registry lookup + regex inside get_plot_num() called by every resolve()
        p = self.plot_registry.get(path) or "A?" if path else "A?"
        self._cached_plot = p
        nums = re.findall(r'\d+', p)
        self._cached_plot_num = int(nums[0]) if nums else 0

    def set_layer(self, layer_name):
        self._current_layer = layer_name

    def get_plot(self):
        return self._cached_plot

    def get_plot_num(self):
        return self._cached_plot_num

    def connection_id(self, code, seq):
        # Kept for any external callers; resolve() inlines this directly.
        return f"{self._cached_plot}_{code}_{seq:02d}"

    def calculate(self, key, pts, geom_type):
        cfg     = self.global_cfg.get("calculated_fields", {}).get(key, {})
        formula = cfg.get("formula", "")
        unit    = cfg.get("unit", "")
        rnd     = cfg.get("round", 2)

        poly_types = ("polygon","block_definition",
                      "block_explode_outer","block_explode_enclose")
        if geom_type in poly_types:
            raw_a = poly_area(pts)
            raw_p = poly_perimeter(pts)
        elif geom_type == "line":
            raw_a, raw_p = 0.0, line_len(pts)
        else:
            raw_a = raw_p = 0.0

        if formula == "area":
            v = {"sqm": raw_a, "hectares": raw_a/10000,
                 "acres": raw_a/4046.856}.get(unit)
        elif formula in ("perimeter", "length"):
            v = {"meters": raw_p, "km": raw_p/1000}.get(unit)
        else:
            v = None

        return round(v, rnd) if v is not None else None

    def derive(self, cfg, props):
        t = cfg.get("transform", "")

        if t == "block_no_from_id":
            val = props.get(cfg.get("from_field","ID_01"), "")
            if val:
                m = re.match(r'B(\d+)', str(val).strip())
                if m:
                    return f"{self.get_plot()}_BLK{int(m.group(1)):02d}"
            return None

        elif t == "block_no_from_layer_name":
            return block_no_from_layer(
                self._current_layer or "", self.get_plot())

        elif t == "strip_last_segment":
            sep = cfg.get("separator", "-")
            val = props.get(cfg.get("from_field",""), "")
            if val and isinstance(val, str):
                parts = val.split(sep)
                return sep.join(parts[:-2]) if len(parts) > 2 else val
            return None

        elif t == "count_filled":
            flds  = cfg.get("from_fields", [])
            vmap  = cfg.get("value_map", {})
            count = sum(1 for f in flds
                        if props.get(f) not in (None, "", "null"))
            return vmap.get(count)

        elif t == "extract_suffix":
            val = props.get(cfg.get("from_field",""), "")
            if val and "_BLK" in val:
                return f"{cfg.get('prefix','')}{val.split('_BLK')[-1]}"
            return None

        elif t == "format_reference_id":
            val    = props.get(cfg.get("from_field",""), "")
            prefix = cfg.get("prefix", "")
            if val and "_BLK" in val:
                return f"{prefix}{self.get_plot()}-BL-{val.split('_BLK')[-1]}"
            return None

        elif t == "prepend_plot":
            fmt = cfg.get("format", "A{plot_no}-{text}")
            val = props.get("block_text", "")
            if val:
                return fmt.format(plot_no=self.get_plot(), text=val)
            return None

        elif t == "block_no_to_connection":
            # A9_BLK01 -> A9-BL01
            val = props.get(cfg.get("from_field", "Block_No"), "")
            if val:
                m = re.match(r'([A-Za-z0-9]+)_BLK([0-9]+)', str(val))
                if m:
                    return f"{m.group(1)}-BL{m.group(2)}"
            return None

        elif t == "block_no_to_prefixed_connection":
            # e.g. ZONE_BLK01 → PREFIX-ZONE-BLK01
            val    = props.get(cfg.get("from_field", "Block_No"), "")
            prefix = cfg.get("prefix", "")
            if val:
                m = re.match(r'([A-Za-z0-9]+)_BLK([0-9]+)', str(val))
                if m:
                    return f"{prefix}{m.group(1)}-BLK{m.group(2)}"
            return None

        elif t == "extract_last_sequence":
            # e.g. PREFIX_ZONE_CODE_01 → PREFIX-01
            val    = props.get(cfg.get("from_field", "Connection_ID"), "")
            prefix = cfg.get("prefix", "")
            pad    = int(cfg.get("pad", 2))
            if val and isinstance(val, str):
                parts = val.split("_")
                last  = parts[-1] if parts else "00"
                try:
                    return f"{prefix}{int(last):0{pad}d}"
                except ValueError:
                    return f"{prefix}{last}"
            return None

        elif t == "auto_sequence":
            # prefix="OFC", pad=2, OBJECTID=3 → "OFC03"
            prefix = cfg.get("prefix", "")
            pad    = int(cfg.get("pad", 2))
            seq_n  = props.get("OBJECTID", 1)
            try:
                return f"{prefix}{int(seq_n):0{pad}d}"
            except (ValueError, TypeError):
                return f"{prefix}{seq_n}"

        return None

    def resolve(self, layer_cfg, raw_props, pts, geom_type, seq, spatial):
        """
        Two-pass field resolution in strict YAML field order.
        Pass 1: constants, block_attrs, spatial, calculate, from_dwg_name,
                from_layer_name
        Pass 2: derive (needs pass-1 values)
        Field order = YAML order = GDB order. Frozen.
        """
        code   = layer_cfg.get("code", "")
        fields = layer_cfg.get("fields", {})

        # Pre-compute once — reused across centroid lookups, Connection_ID
        # seed, and the post-Pass-1 Connection_ID re-derivation.
        _ct        = centroid(pts)
        _cid_clean = code

        # Seed props with OBJECTID + placeholder Connection_ID in one shot.
        # Pre-fill all field slots to None — single dict literal, no loop needed.
        props = {"OBJECTID": seq,
                 "Connection_ID": f"{self._cached_plot}_{_cid_clean}_{seq:02d}",
                 **{fn: None for fn in fields if fn not in ("OBJECTID", "Connection_ID")}}

        # Pass 1
        for fn, fc in fields.items():
            if fn in ("OBJECTID", "Connection_ID"):
                continue

            if fc is None:
                props[fn] = None

            elif isinstance(fc, (str, int, float, bool)):
                props[fn] = fc

            elif isinstance(fc, dict):

                if fc.get("from_dwg_name"):
                    _plot = self.get_plot()
                    # If the DWG filename already encodes a sub-plot suffix
                    # (lowercase letter at end, e.g. 'A9a', 'A9b'), trust it.
                    # If not, resolve the correct sub-plot by
                    # finding the spatial reference polygon that contains this
                    # feature's centroid.
                    if _plot and not _RE_HAS_SUBSUFFIX.search(_plot):
                        _spatial_plot = spatial.plot_from_position(_ct)
                        if _spatial_plot:
                            # Log once per (DWG, layer) — not per feature —
                            # to avoid flooding stdout for every feature
                            _lk = (self._current_dwg, self._current_layer)
                            if _lk not in self._plot_log_seen:
                                Logger.info(
                                    f"  Plot_No: '{_plot}' → '{_spatial_plot}' "
                                    f"(spatial — DWG spans multiple sub-plots)"
                                )
                                self._plot_log_seen.add(_lk)
                            _plot = _spatial_plot
                    props[fn] = _plot

                elif fc.get("from_layer_name"):
                    props[fn] = block_no_from_layer(
                        self._current_layer or "", self.get_plot())

                elif "from_config" in fc:
                    # Reads a value directly from global config by key name.
                    # eg from_config: ht_cable_end_connection
                    #    → global_cfg["ht_cable_end_connection"]
                    _cfg_key = fc["from_config"]
                    props[fn] = self.global_cfg.get(_cfg_key)

                elif "calculate" in fc:
                    props[fn] = self.calculate(fc["calculate"], pts, geom_type)

                elif "from_attr" in fc:
                    tag = str(fc["from_attr"]).upper()
                    props[fn] = raw_props.get(tag, raw_props.get(fc["from_attr"], " "))

                elif isinstance(fc, dict) and fc.get("from_sm_attachment"):
                    props[fn] = raw_props.get("_sm_attachment", None)

                elif isinstance(fc, dict) and "conditional" in fc:
                    # conditional: if_sub_type: "HT" → then: ... else: null
                    # Used for Attachment on HT trenches — value comes from
                    # _sm_attachment set during line splitting above.
                    _cond     = fc["conditional"]
                    _sub_type = raw_props.get("_sub_type", "")
                    if _cond.get("if_sub_type") == _sub_type:
                        # Value was already stamped as _sm_attachment during split
                        props[fn] = raw_props.get("_sm_attachment", None)
                    else:
                        props[fn] = None

                elif isinstance(fc, dict) and "from_merge_source" in fc:
                    key = fc["from_merge_source"]
                    if key == "code":
                        props[fn] = raw_props.get("_ms_code") or layer_cfg.get("code", "")
                    elif key == "sub_classification":
                        props[fn] = raw_props.get("_ms_sub_class") or None
                    else:
                        props[fn] = raw_props.get(f"_ms_{key}") or None

                elif "spatial_join" in fc:
                    jt = fc["spatial_join"]
                    if jt == "primary":
                        bc = self.global_cfg["block_no"]["primary_source"]
                        props[fn] = spatial.nearest(
                            _ct, bc["from_layer"], bc["from_field"])
                    elif jt == "secondary":
                        bc        = self.global_cfg["block_no"]["secondary_source"]
                        bl_layer  = bc["from_layer"]
                        bl_field  = bc["from_field"]
                        _cur_plot = props.get("Plot_No")
                        props[fn] = spatial.nearest_in_plot(_ct, bl_layer, bl_field, _cur_plot)
                    elif isinstance(jt, dict):
                        _method = jt.get("method", "nearest")
                        _lyr    = jt.get("from_layer", "")
                        _fld    = jt.get("from_field", "")
                        _rad    = jt.get("radius_m", None)

                        # nearest_exclusive is handled in a post-extraction batch pass
                        # (assign_exclusive in main) — skip here, leave as placeholder
                        if _method == "nearest_exclusive":
                            props[fn] = " "
                            continue
                        if _method == "nearest_endpoint":
                            _sv, _ev = spatial.nearest_endpoint(pts, _lyr, _fld, _rad)
                            _raw = _sv if "start" in fn.lower() else _ev if "end" in fn.lower() else _sv
                        else:
                            _raw = spatial.nearest(_ct, _lyr, _fld)
                        # Diagnostic — warn once per (field, from_layer) when join
                        # returns nothing; avoids flooding stdout for every feature.
                        if _raw is None or _raw == " ":
                            _miss_key = (fn, _lyr)
                            if _miss_key not in self._miss_warned:
                                self._miss_warned.add(_miss_key)
                                _cache_keys = list(spatial.cache.keys())
                                if _lyr not in _cache_keys:
                                    Logger.warn(
                                        f"  spatial_join MISS: '{fn}' — layer '{_lyr}' not in cache. "
                                        f"Available: {_cache_keys[:8]}"
                                    )
                                else:
                                    Logger.warn(
                                        f"  spatial_join MISS: '{fn}' — layer '{_lyr}' found in cache "
                                        f"but field '{_fld}' returned empty"
                                    )
                        # Optional transform — format string with join result + feature props
                        # eg format: "{Plot_No}-BL{Ref_No}"
                        _tfm = jt.get("transform", {})
                        _fmt = _tfm.get("format", "") if isinstance(_tfm, dict) else ""
                        if _fmt and _raw is not None and _raw != " ":
                            _subs = {k: (v or "") for k, v in props.items()}
                            _raw_str = str(_raw)
                            # Only strip simple "PREFIX-digits" values (e.g. "REF-01" → "01")
                            # Preserve compound values like "ZONE_BLK01" intact
                            _m_strip = re.match(r'^[A-Za-z]+[-_]+0*(\d+)$', _raw_str)
                            _subs[_fld] = _m_strip.group(1) if _m_strip else _raw_str
                            try:
                                result = _fmt.format_map(_subs)
                                # Normalise A9a_BLK01 → A9a-BLK01 inside formatted value
                                result = re.sub(r'([A-Za-z]\d+[a-z]?)_BLK', r'\1-BLK', result)
                                props[fn] = result
                            except (KeyError, ValueError) as _fmte:
                                Logger.warn(f"spatial_join transform failed for {fn}: fmt='{_fmt}' err={_fmte}")
                                props[fn] = _raw
                        else:
                            props[fn] = _raw

                elif "block_attr" in fc:
                    tag = fc["block_attr"].upper()
                    val = raw_props.get(tag)
                    if val is None:
                        for fb in fc.get("fallbacks", []):
                            val = raw_props.get(fb.upper())
                            if val is not None:
                                break
                    props[fn] = val or None

                elif "derive" in fc:
                    pass   # handled in pass 2

        # Re-derive Connection_ID with the fully-resolved Plot_No and the
        # most-specific code available.  Priority:
        #   1. raw_props["_ms_code"] — per-source code stamped by merge_source config
        #   2. props["Code"] — resolved Code field (same value for most layers)
        #   3. _cid_clean   — layer-level code fallback
        # Reading raw_props directly avoids any dependency on the Code field's
        # YAML key name or position in the field list.
        _final_plot = str(props.get("Plot_No") or "").strip()
        if _final_plot and _final_plot not in (" ", ""):
            _ms_raw = str(raw_props.get("_ms_code") or "").strip()
            if _ms_raw:
                _cid_from_code = _ms_raw
            else:
                _resolved_code = str(props.get("Code") or "").strip()
                _cid_from_code = _resolved_code if _resolved_code not in (" ", "") else _cid_clean
            props["Connection_ID"] = f"{_final_plot}_{_cid_from_code}_{seq:02d}"

        # Pass 2 — derived fields (source fields now populated)
        for fn, fc in fields.items():
            if isinstance(fc, dict) and "derive" in fc:
                props[fn] = self.derive(fc["derive"], props)

        # Strip underscore-prefixed working fields AND replace None with " " in one pass
        return {k: (" " if v is None else v)
                for k, v in props.items()
                if not k.startswith("_")}


# ============================================================
# LAYER EXTRACTOR
# ============================================================

class LayerExtractor:

    def __init__(self, acad, field_engine, spatial, global_cfg, dwg_paths):
        self.acad    = acad
        self.fe      = field_engine
        self.spatial = spatial
        self.gcfg    = global_cfg
        self.paths   = dwg_paths

    def _get_msp_doc(self, dwg_key):
        path = self.paths.get(dwg_key)
        if not path:
            Logger.err(f"DWG key '{dwg_key}' not in config")
            return None, None, None
        self.fe.set_dwg(path)
        return self.acad.get_modelspace(path), self.acad.get_doc(path), path

    def extract(self, layer_cfg):
        name      = layer_cfg["name"]
        geom_type = layer_cfg.get("geometry", "polygon")
        Logger.step("Extracting: " + name)
        merge_sources = layer_cfg.get("merge_sources")
        if merge_sources:
            sources = []
            for ms in merge_sources:
                raw_key = ms.get("source_dwg") or layer_cfg.get("source_dwg", "")
                keys = raw_key if isinstance(raw_key, list) else [raw_key]
                for dk in keys:
                    if not dk:
                        continue
                    sources.append({
                        "dwg_key":    dk,
                        "src_layer":  ms.get("source_layer", ""),
                        "fallbacks":  ms.get("fallbacks", []),
                        "match_mode": ms.get("match_mode",
                                      layer_cfg.get("match_mode", "prefix")),
                        "sub_type":   ms.get("sub_type", ""),
                        "code":       ms.get("code", ""),
                        "sub_classification": ms.get("sub_classification", ""),
                    })
        else:
            sources = [{
                "dwg_key":    layer_cfg.get("source_dwg", ""),
                "src_layer":  layer_cfg.get("source_layer", ""),
                "fallbacks":  layer_cfg.get("fallbacks", []),
                "match_mode": layer_cfg.get("match_mode", "exact"),
            }]
        all_features = []
        seq = 0
        for source in sources:
            dwg_key    = source["dwg_key"]
            src_layer  = source["src_layer"]
            fallbacks  = source["fallbacks"]
            match_mode = source["match_mode"]
            if not dwg_key or not src_layer:
                continue
            msp, doc, path = self._get_msp_doc(dwg_key)
            if not msp:
                Logger.warn("  '" + dwg_key + "' not open in AutoCAD - skipping")
                continue
            matched_layers = find_layers(msp, src_layer, fallbacks, match_mode)
            if not matched_layers:
                Logger.warn("  Layer '" + src_layer + "' not found in '" + dwg_key + "'")
                continue
            Logger.info("  " + dwg_key + " -> " + str(len(matched_layers)) + " layer(s)")
            for layer_name in matched_layers:
                self.fe.set_layer(layer_name)
                entities = get_entities_on_layer(msp, layer_name)
                total = len(entities)
                skipped = 0
                er = _er   # module-level singleton — no instance state
                # Type counts from index (no extra COM scan)
                _midx = get_msp_index(msp)
                type_counts = _midx.get('__types__', {}).get(layer_name, {})
                Logger.info("  Entity types in '" + layer_name + "': " + str(type_counts))

                # ── Hoist all per-layer constants out of the entity loop ──────
                # Every layer_cfg / source lookup that doesn't vary per entity
                # is moved here so the hot inner loop only does real work.
                _gparams        = layer_cfg.get("geometry_params", {})
                _src_sub_type   = source.get("sub_type",           "")
                _src_ms_code    = source.get("code",               "")
                _src_ms_sub_cls = source.get("sub_classification", "")
                _progress_lbl   = f"{name} [{layer_name}]"

                # Detect sub-type suffix from the matched DWG layer name.
                # e.g. "Layer (AC)" → "AC", "Layer (DC)" → "DC"
                # Falls back to sub_type defined in YAML merge_sources config.
                # Stored as _ms_layer_subtype so YAML can expose it via
                #   { from_merge_source: layer_subtype }
                _ln_up = layer_name.upper()
                if   " AC" in _ln_up or "(AC)" in _ln_up or "_AC" in _ln_up:
                    _src_layer_subtype = "AC"
                elif " DC" in _ln_up or "(DC)" in _ln_up or "_DC" in _ln_up:
                    _src_layer_subtype = "DC"
                elif _src_sub_type:
                    _src_layer_subtype = _src_sub_type   # sub_type from config
                else:
                    _src_layer_subtype = ""

                # Geometry-type guard flags (checked on every entity)
                _gp_only_lwpoly = _gparams.get("only_lwpolyline",    False)
                _gp_only_insert = _gparams.get("only_insert",        False)
                _gp_block_name  = _gparams.get("block_name",         "")
                _gp_fr_deg      = _gparams.get("forced_rotation_deg", None)
                _gp_fr_rad      = (math.radians(float(_gp_fr_deg))
                                   if _gp_fr_deg is not None else None)
                # Polygon / area-filter params
                _gp_target_sqm  = float(_gparams.get("target_area_sqm", 0))
                _gp_tol         = float(_gparams.get("tolerance",        5.0))
                _gp_min_area    = float(_gparams.get("min_area_sqm",     0))
                _gp_vertex_count = int(_gparams.get("vertex_count",      0))
                _gp_use_width   = _gparams.get("use_polyline_width", False)
                _gp_apply_to    = _gparams.get("apply_width_to",     None)
                _gp_half_width  = float(_gparams.get("half_width_m",  0.025))
                _gp_sm_layer    = _gparams.get("section_mark_layer")
                _gp_sm_thresh   = float(_gparams.get("snap_threshold_m", 50))
                _gp_rotate_90   = bool(_gparams.get("rotate_90",         False))
                _gp_linetype    = str(_gparams.get("linetype",           "")).strip().upper()
                _gp_fixed_pts   = _gparams.get("local_pts",              [])  # for block_explode_fixed
                # block_explode_enclose uses a different tolerance default
                _gp_be_tol      = float(_gparams.get("tolerance", 10))

                # ── Hoist section-mark split config for line geometry ─────────
                # Previously scanned layer_cfg["fields"] on every entity.
                # Computed once here — None if this source/geometry doesn't use it.
                _sm_split_cfg = None
                _sm_list_cache = None   # built once on first use
                if geom_type == "line":
                    for _fn0, _fc0 in layer_cfg.get("fields", {}).items():
                        if isinstance(_fc0, dict):
                            _cond0 = _fc0.get("conditional", {})
                            if (_cond0.get("if_sub_type") == _src_sub_type
                                    and "then" in _cond0
                                    and isinstance(_cond0["then"], dict)
                                    and _cond0["then"].get("method") == "segment_split_by_section_marks"):
                                _sm_cfg0    = _cond0["then"]
                                _sm_split_cfg = (_fn0, _sm_cfg0)
                                # Build sm_list once from cache
                                _sm_lyr0    = _sm_cfg0.get("section_layer", "")
                                _sm_thresh0 = float(_sm_cfg0.get("snap_threshold_m", 2.0))
                                _sm_raw0    = self.spatial.cache.get(_sm_lyr0, [])
                                if not _sm_raw0:
                                    for _fb0 in _sm_cfg0.get("fallbacks", []):
                                        _sm_raw0 = self.spatial.cache.get(_fb0, [])
                                        if _sm_raw0:
                                            break
                                _sm_list_cache = [
                                    {"pos": f["centroid"],
                                     "label": (f.get("properties", {}).get("label", "")
                                               or f.get("properties", {}).get("Section_Marks", "")
                                               or "")}
                                    for f in _sm_raw0 if f.get("centroid")
                                ]
                                break
                # ─────────────────────────────────────────────────────────────
                for _ei, _ep in enumerate(entities):
                    et, ent = _ep
                    Logger.progress(_ei + 1, total, _progress_lbl)
                    pts = None
                    raw_props = {
                        "_sub_type":       _src_sub_type,
                        "_ms_code":        _src_ms_code,
                        "_ms_sub_class":   _src_ms_sub_cls,
                        "_ms_layer_subtype": _src_layer_subtype,
                    }
                    try:
                        if geom_type == "polygon":
                            # only_lwpolyline: true → skip INSERT entities on this layer
                            if _gp_only_lwpoly and et != "LWPOLYLINE":
                                skipped += 1
                                continue
                            if et == "LWPOLYLINE":
                                # linetype filter — only accept entities with matching linetype
                                if _gp_linetype:
                                    try:
                                        _ent_lt = str(ent.Linetype).strip().upper()
                                        if _ent_lt != _gp_linetype:
                                            skipped += 1
                                            continue
                                    except Exception:
                                        pass
                                _do_wid = (_gp_use_width and
                                           (_gp_apply_to is None or _src_sub_type in _gp_apply_to))
                                _cl      = er.lwpoly_coords(ent)
                                if _do_wid and _cl:
                                    try:    _hw = ent.ConstantWidth / 2.0
                                    except: _hw = _gp_half_width
                                    if _hw < 1e-6: _hw = _gp_half_width
                                    pts = er.lwpoly_to_polygon(_cl, _hw)
                                    # SM splitting on wide polygons
                                    if _gp_sm_layer:
                                        _sm_raw  = self.spatial.cache.get(_gp_sm_layer, [])
                                        _sm_list = [{"pos":f["centroid"],"label":f.get("properties",{}).get("label","")}
                                                    for f in _sm_raw if f.get("centroid") and f.get("properties",{}).get("label","")]
                                        if _sm_list:
                                            _segs   = EntityReader.split_by_section_marks(_cl, _sm_list, _gp_sm_thresh)
                                            if len(_segs) > 1:
                                                for _seg in _segs:
                                                    _sp   = er.lwpoly_to_polygon(_seg["pts"], _hw)
                                                    if not _sp or len(_sp) < 3: continue
                                                    _rp2  = dict(raw_props); _rp2["_sm_attachment"] = _seg["label"]
                                                    seq  += 1
                                                    _pr2  = self.fe.resolve(layer_cfg, _rp2, _sp, "polygon", seq, self.spatial)
                                                    _ct2  = (sum(p[0] for p in _sp[:-1])/max(1,len(_sp)-1),
                                                             sum(p[1] for p in _sp[:-1])/max(1,len(_sp)-1))
                                                    all_features.append({"type":"Feature",
                                                        "geometry":{"type":"Polygon","coordinates":[[[p[0],p[1]] for p in _sp]]},
                                                        "properties":_pr2,"_centroid":_ct2})
                                                continue
                                else:
                                    pts = _cl
                                # Auto-close open polylines
                                if pts and pts[0] != pts[-1]:
                                    pts = pts + [pts[0]]
                                # Apply area filter if geometry_params specified
                                if pts:
                                    # Vertex count filter — exact structural match
                                    if _gp_vertex_count > 0:
                                        _vn = len(pts)
                                        if _vn != _gp_vertex_count and _vn != _gp_vertex_count + 1:
                                            skipped += 1
                                            continue
                                        # Log every vertex_count match with its area (all 3 scales)
                                        # so we can see the full range of compound boundary areas
                                        _a_dbg = poly_area(pts)
                                        Logger.info(f"    v{_vn} match #{_ei}: area={_a_dbg:.4f} /1e4={_a_dbg/1e4:.4f} /1e6={_a_dbg/1e6:.6f}")
                                    if _gp_min_area > 0 or (_gp_target_sqm > 0 and _gp_vertex_count == 0):
                                        a = poly_area(pts)
                                        # Minimum area — reject tiny detail lines/polygons
                                        if _gp_min_area > 0:
                                            _a_scaled = min(a, a/1e4, a/1e6, key=lambda x: abs(x))
                                            # Use raw area — if too small at all scales, skip
                                            if a < _gp_min_area and (a/1e4) < _gp_min_area and (a/1e6) < _gp_min_area:
                                                skipped += 1
                                                continue
                                        if _gp_target_sqm > 0:
                                            if _ei < 5 and a > 0:
                                                Logger.info(f"    area filter: actual={a:.4f} actual/1e4={a/1e4:.4f} actual/1e6={a/1e6:.6f} target={_gp_target_sqm} tol={_gp_tol}")
                                            candidates = [a, a/1e4, a/1e6]
                                            best = min(candidates, key=lambda x: abs(x - _gp_target_sqm))
                                            if abs(best - _gp_target_sqm) > _gp_tol:
                                                skipped += 1
                                                continue
                                    # rotate_90: rotate polygon 90° around its centroid
                                    # (X,Y) → (-Y+cy+cx, X-cx+cy) = rotate CCW 90° in place
                                    if _gp_rotate_90 and pts:
                                        _cx = sum(p[0] for p in pts) / len(pts)
                                        _cy = sum(p[1] for p in pts) / len(pts)
                                        pts = [(_cx - (p[1] - _cy),
                                                _cy + (p[0] - _cx)) for p in pts]
                            elif et == "POLYLINE":
                                pts = er.poly_coords(ent)
                                if pts:
                                    _a_p = poly_area(pts)
                                    Logger.info(f"    POLYLINE #{_ei}: verts={len(pts)} area={_a_p:.4f} /1e4={_a_p/1e4:.4f}")
                            elif et == "INSERT":
                                pts = er.block_def_coords(doc, ent.Name, ent)
                                raw_props = er.block_attrs(ent)
                                if not pts:
                                    skipped += 1
                                    continue
                            else:
                                skipped += 1
                                continue
                        elif geom_type == "line":
                            if et == "LWPOLYLINE":
                                pts = er.lwpoly_coords(ent, force_close=False)
                            elif et == "POLYLINE":
                                pts = er.poly_coords(ent)
                            elif et == "LINE":
                                pts = er.line_coords(ent)
                            else:
                                skipped += 1
                                continue
                            # ── Section mark splitting for HT trenches ───────
                            # If any field has conditional.if_sub_type matching
                            # this source's sub_type, split the line at section
                            # mark positions and emit one feature per segment
                            # with the section mark label as Attachment value.
                            if pts and len(pts) >= 2:
                                if _sm_split_cfg and _sm_list_cache:
                                    _sm_fn, _sm_cfg = _sm_split_cfg
                                    _sm_thresh = _sm_thresh0
                                    _segs = EntityReader.split_by_section_marks(
                                        pts, _sm_list_cache, _sm_thresh)
                                    if len(_segs) > 1:
                                        for _seg in _segs:
                                            _sp = _seg["pts"]
                                            if not _sp or len(_sp) < 2:
                                                continue
                                            _rp2 = dict(raw_props)
                                            _rp2["_sm_attachment"] = _seg["label"]
                                            seq += 1
                                            _pr2 = self.fe.resolve(
                                                layer_cfg, _rp2, _sp, "line", seq, self.spatial)
                                            _ct2 = centroid(_sp)
                                            _fe2 = {
                                                "type": "Feature",
                                                "geometry": {"type": "LineString",
                                                             "coordinates": [[p[0], p[1]] for p in _sp]},
                                                "properties": _pr2,
                                                "_centroid": _ct2,
                                                "_pts": _sp,
                                            }
                                            all_features.append(_fe2)
                                        continue   # skip normal single-feature append
                        elif geom_type == "point":
                            if et in ("POINT", "INSERT"):
                                pts = er.point_coords(ent)
                                raw_props = er.block_attrs(ent)
                            elif et == "LWPOLYLINE":
                                # Single-vertex or collapsed polyline used as point marker
                                try:
                                    cr  = list(ent.Coordinates)
                                    vts = [(cr[i], cr[i+1]) for i in range(0, len(cr), 2)]
                                    if vts:
                                        cx = sum(v[0] for v in vts) / len(vts)
                                        cy = sum(v[1] for v in vts) / len(vts)
                                        pts = [(cx, cy)]
                                except Exception:
                                    skipped += 1
                                    continue
                            elif et == "CIRCLE":
                                try:
                                    c = ent.Center
                                    pts = [(c[0], c[1])]
                                except Exception:
                                    skipped += 1
                                    continue
                            elif et in ("TEXT", "MTEXT"):
                                try:
                                    c = ent.InsertionPoint
                                    pts = [(c[0], c[1])]
                                except Exception:
                                    skipped += 1
                                    continue
                            else:
                                skipped += 1
                                continue
                        elif geom_type == "block_definition":
                            # only_insert: true → skip LWPOLYLINE entities on this layer
                            if _gp_only_insert and et != "INSERT":
                                skipped += 1
                                continue
                            if et == "INSERT":
                                EntityReader._geom_type_hint = "block_explode_enclose" if _gp_fr_deg is not None else ""
                                EntityReader._forced_rotation_rad = _gp_fr_rad
                                pts = er.block_def_coords(doc, ent.Name, ent)
                                EntityReader._geom_type_hint      = ""
                                EntityReader._forced_rotation_rad = None
                                raw_props = er.block_attrs(ent)
                                if not pts:
                                    skipped += 1
                                    continue
                            else:
                                skipped += 1
                                continue
                        elif geom_type == "block_explode_outer":
                            if et != "INSERT":
                                skipped += 1
                                continue
                            if _gp_block_name and ent.Name != _gp_block_name:
                                skipped += 1
                                continue
                            EntityReader._forced_rotation_rad = _gp_fr_rad
                            pts = er.block_def_coords(doc, ent.Name, ent)
                            EntityReader._forced_rotation_rad = None
                            raw_props = er.block_attrs(ent)
                            raw_props["block_text"] = er.block_text(ent)
                            if not pts:
                                skipped += 1
                                continue
                        elif geom_type == "block_explode_fixed":
                            if et != "INSERT":
                                skipped += 1
                                continue
                            if _gp_block_name and ent.Name != _gp_block_name:
                                skipped += 1
                                continue
                            if not _gp_fixed_pts:
                                Logger.warn("block_explode_fixed: no local_pts in geometry_params")
                                skipped += 1
                                continue
                            pts = er.block_fixed_coords(ent, _gp_fixed_pts, _gp_fr_deg)
                            raw_props = er.block_attrs(ent)
                            if not pts:
                                skipped += 1
                                continue
                                skipped += 1
                                continue
                            if _gp_block_name and ent.Name != _gp_block_name:
                                skipped += 1
                                continue
                            # Log INSERT block names on first few entities when no
                            # block_name filter — reveals actual block names in DWG
                            if not _gp_block_name and _ei < 5:
                                try:
                                    Logger.info(f"    INSERT block name: '{ent.Name}'")
                                except Exception:
                                    pass
                            EntityReader._target_area         = _gp_target_sqm
                            EntityReader._geom_type_hint      = "block_explode_enclose"
                            EntityReader._forced_rotation_rad = _gp_fr_rad
                            pts       = er.block_def_coords(doc, ent.Name, ent)
                            EntityReader._target_area         = 0
                            EntityReader._geom_type_hint      = ""
                            EntityReader._forced_rotation_rad = None
                            raw_props = er.block_attrs(ent)
                            raw_props["block_text"] = er.block_text(ent)
                            if not pts:
                                skipped += 1
                                continue
                            # area filter not needed here — correct polyline
                            # already selected inside block_def_coords via target hint
                        if not pts:
                            skipped += 1
                            continue
                        # Capture INSERT point for block entities
                        # Used by assign_exclusive to match line termini precisely
                        _insert_pt = None
                        _attr_pts  = {}   # {TAG: (world_x, world_y)} for each attribute
                        if et == "INSERT":
                            try:
                                _ip = ent.InsertionPoint
                                _insert_pt = (_ip[0], _ip[1])
                            except Exception:
                                pass
                            # Per-attribute world positions — used by derive_from: parent_layer
                            # to place each child feature at its attribute's text location
                            _attr_pts = er.block_attr_positions(ent)
                        seq += 1
                        props = self.fe.resolve(
                            layer_cfg, raw_props, pts, geom_type, seq, self.spatial)
                        poly_types = ("polygon", "block_definition",
                                      "block_explode_outer", "block_explode_enclose")
                        if geom_type in poly_types:
                            geom_obj = {"type": "Polygon",
                                        "coordinates": [[[p[0], p[1]] for p in pts]]}
                        elif geom_type == "line":
                            geom_obj = {"type": "LineString",
                                        "coordinates": [[p[0], p[1]] for p in pts]}
                        else:
                            geom_obj = {"type": "Point",
                                        "coordinates": [pts[0][0], pts[0][1]]}
                        _feat_entry = {
                            "type":       "Feature",
                            "geometry":   geom_obj,
                            "properties": props,
                            "_centroid":  centroid(pts),
                            "_pts":       pts,   # kept for nearest_exclusive post-pass; stripped before write
                        }
                        if _insert_pt:
                            _feat_entry["_insert_pt"] = _insert_pt
                        if _attr_pts:
                            _feat_entry["_attr_pts"] = _attr_pts  # per-attr world coords; stripped before write
                        all_features.append(_feat_entry)
                    except Exception as e:
                        Logger.warn("Entity error: " + str(e))
                        skipped += 1
                        continue
        Logger.ok("Extracted " + str(seq) + " features from '" + name + "'")
        return all_features


def _dissolve_to_clean_polygon(polys, close_gap=200.0):
    """
    Dissolve a list of ShapelyPolygons into one clean exterior-only Polygon.

    Strategy:
      1. unary_union — merge all blocks
      2. buffer(+close_gap) — flood all gaps, notches, and concavities
      3. unary_union — ensure single geometry after expand
      4. buffer(-close_gap) — shrink back to approximate original edge
      5. unary_union — clean artefacts
      6. Take largest polygon if still MultiPolygon
      7. Rebuild from exterior ring only — zero holes guaranteed

    close_gap=200m fills inter-block road gaps (~10-30m), the A9a/A9b
    shared-boundary notch, and any outer concavities, while staying well
    within a single block footprint (~250m × 500m).
    """
    if not polys:
        return None
    try:
        merged = unary_union(polys)
        # Expand — seals all gaps and fills concavities
        merged = merged.buffer(close_gap, join_style=2)
        merged = unary_union(merged)
        # Shrink back — restores true outer edge positions
        merged = merged.buffer(-close_gap, join_style=2)
        merged = unary_union(merged)
        # Force single polygon
        if merged.geom_type == "MultiPolygon":
            merged = max(merged.geoms, key=lambda g: g.area)
        # Strip ALL interior rings — exterior trace only.
        # buffer(-close_gap) can reintroduce holes where bridged gaps
        # were smaller than close_gap. Fill them with a second small
        # expand+shrink pass before extracting the exterior ring.
        if merged.interiors and len(list(merged.interiors)) > 0:
            merged = ShapelyPolygon(merged.exterior.coords).buffer(1).buffer(-1)
            if merged.geom_type == "MultiPolygon":
                merged = max(merged.geoms, key=lambda g: g.area)
        return ShapelyPolygon(merged.exterior.coords)
    except Exception as exc:
        Logger.err(f"  dissolve error: {exc}")
        return None


def derive_zone_boundary_from_reference(bb_feats, pb_cfg, output_dir, crs="EPSG:32642"):
    """
    Auto-derive a zone boundary layer — one dissolved feature per zone declared in merge_sources.

    Output: 1 layer, N features — each zone has its OWN dissolved geometry.
    Zone membership is determined from Block_No prefix (ZONE_BLK01 → ZONE) — reliable
    regardless of whether the zone ID was stamped on the reference features.
    """
    if not HAS_SHAPELY:
        Logger.err("Plot Boundary auto-derive requires shapely. Run: pip install shapely")
        return []

    _layer_label = pb_cfg.get("name", "Zone Boundary")
    if not bb_feats:
        Logger.warn(f"{_layer_label}: no reference features available — cannot derive")
        return []

    Logger.step(f"{_layer_label}: dissolving per zone → distinct geometry per feature")

    merge_sources = pb_cfg.get("merge_sources", [])
    fields        = pb_cfg.get("fields", {})
    code_val      = fields.get("Code", "")
    sub_plots     = [ms.get("sub_plot", "") for ms in merge_sources if ms.get("sub_plot")]

    if not sub_plots:
        Logger.warn(f"{_layer_label}: no sub_plots declared in merge_sources — nothing to derive")
        return []

    # Build per-sub-plot polygon lists — keyed by Block_No prefix
    sub_plot_polys = {sp.strip(): [] for sp in sub_plots}

    for feat in bb_feats:
        props_bb = feat.get("properties", {})
        # Primary: derive sub-plot from Block_No prefix — always present and correct
        bn = str(props_bb.get("Block_No", "") or "").strip()
        if "_BLK" in bn:
            sp_val = bn.split("_BLK")[0]
        else:
            # Fallback: Sub_Plot stamp or sub_plot field
            sp_val = (props_bb.get("Sub_Plot") or props_bb.get("sub_plot") or "").strip()

        # Build shapely polygon from geometry or _pts
        pts = None
        geom   = feat.get("geometry") or {}
        coords = geom.get("coordinates")
        if coords:
            try:
                pts = ShapelyPolygon(coords[0])
            except Exception:
                pass
        else:
            raw = feat.get("_pts", [])
            if raw and len(raw) >= 3:
                try:
                    pts = ShapelyPolygon(raw)
                except Exception:
                    pass

        if pts is None:
            continue

        if sp_val in sub_plot_polys:
            sub_plot_polys[sp_val].append(pts)
        else:
            Logger.warn(f"  Block_No '{bn}' sub-plot '{sp_val}' not in declared sub_plots "
                        f"{sub_plots} — skipping")

    features = []
    for seq, sp in enumerate(sub_plots, start=1):
        sp_clean = sp.strip()
        polys    = sub_plot_polys.get(sp_clean, [])

        if not polys:
            Logger.warn(f"  {_layer_label}: no reference blocks found for zone '{sp_clean}' — skipping")
            continue

        Logger.info(f"  Dissolving {len(polys)} blocks for {sp_clean}...")
        clean = _dissolve_to_clean_polygon(polys, close_gap=300.0)
        if clean is None:
            Logger.err(f"  {_layer_label}: dissolve failed for {sp_clean}")
            continue

        shell_coords = list(clean.exterior.coords)
        if shell_coords[0] != shell_coords[-1]:
            shell_coords = shell_coords + [shell_coords[0]]

        try:
            s_poly       = ShapelyPolygon(shell_coords)
            area_sqm     = round(s_poly.area, 2)
            area_ha      = round(area_sqm / 10_000, 2)
            area_ac      = round(area_sqm / 4_046.856, 2)
            perimeter_m  = round(s_poly.length, 2)
            perimeter_km = round(perimeter_m / 1_000, 2)
        except Exception:
            area_ha = area_ac = perimeter_km = None

        cx = sum(c[0] for c in shell_coords) / len(shell_coords)
        cy = sum(c[1] for c in shell_coords) / len(shell_coords)

        blk_count = len(polys)
        conn_id   = fields.get("Connection_ID", sp_clean)

        props = {
            "OBJECTID":           seq,
            "Plant_Name":         fields.get("Plant_Name",     ""),
            "Code":               code_val,
            "Connection_ID":      conn_id,
            "Classification":     fields.get("Classification", _layer_label),
            "Plot_No":            sp_clean,
            "Total_Blocks":       blk_count,
            "Total_Capacity_MW":  fields.get("Total_Capacity_MW", " "),
            "Area_Ha":            area_ha,
            "Area_Ac":            area_ac,
            "Perimeter_Km":       perimeter_km,
            "Owned_By":           fields.get("Owned_By",      " "),
            "Prepared_By":        fields.get("Prepared_By",   " "),
            "Country":            fields.get("Country",       ""),
            "State":              fields.get("State",         ""),
            "District":           fields.get("District",      ""),
            "Taluka":             fields.get("Taluka",        ""),
            "Village":            fields.get("Village",       ""),
            "Jurisdiction":       fields.get("Jurisdiction",  ""),
            "Attachment":         " ",
        }

        features.append({
            "type":     "Feature",
            "geometry": {
                "type":        "Polygon",
                "coordinates": [[[c[0], c[1]] for c in shell_coords]]
            },
            "properties": props,
            "_centroid":  (cx, cy),
            "_pts":       shell_coords,
        })
        Logger.ok(f"  {sp_clean}: {blk_count} blocks | {area_ha} ha | conn={conn_id}")

    Logger.ok(f"{_layer_label}: {len(features)} features with distinct geometries "
              f"({', '.join(sub_plots)})")
    return features


def write_geojson(features, output_path, crs="EPSG:32642"):
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    clean = [{k: v for k, v in f.items() if k not in ("_centroid", "_pts", "_attr_pts", "_insert_pt")}
             for f in features]
    fc = {
        "type": "FeatureCollection",
        "name": os.path.splitext(os.path.basename(output_path))[0],
        "crs": {
            "type": "name",
            "properties": {"name": f"urn:ogc:def:crs:EPSG::{crs.split(':')[-1]}"}
        },
        "features": clean
    }
    with open(output_path, "w", encoding="utf-8") as fp:
        # Compact separators for large outputs (no trailing spaces, no indent).
        # indent=2 is 3-5x slower and produces files 30-40% larger.
        # QGIS/ArcGIS read both formats identically.
        if len(clean) > 500:
            json.dump(fc, fp, ensure_ascii=False, separators=(',', ':'))
        else:
            json.dump(fc, fp, ensure_ascii=False, indent=2)
    Logger.ok(f"Saved → {output_path}  ({len(clean)} features)")


# ============================================================
# MMS BLOCK NUMBERING REGISTRATION
# ============================================================

def register_mms_block_numbering(acad, global_cfg, spatial, dwg_paths, plot_registry):
    bc     = global_cfg.get("block_no", {}).get("primary_source", {})
    keys   = bc.get("from_dwg", "array_layout")
    layer  = bc.get("from_layer", "MMS Block Numbering")
    field  = bc.get("from_field", "Contents")
    fbacks = bc.get("fallbacks", [])
    if isinstance(keys, str):
        keys = [keys]
    er = _er   # module-level singleton
    all_features = []
    for key in keys:
        path = dwg_paths.get(key)
        if not path:
            Logger.warn("MMS: key '" + key + "' not in config"); continue
        plot_id = plot_registry.get(path) or "A?"
        msp = acad.get_modelspace(path)
        if not msp:
            Logger.warn("MMS: could not open '" + key + "'"); continue
        matched = find_layers(msp, layer, fbacks, "exact")
        if not matched:
            Logger.warn("MMS Block Numbering not found in '" + key + "'"); continue
        count = 0
        for ent in msp:
            try:
                if ent.Layer != matched[0]: continue
                if er.etype(ent) in ("MTEXT", "TEXT"):
                    raw = er.mtext_content(ent)
                    fmt = format_block_no(raw, plot_id)
                    try:
                        pt = ent.InsertionPoint
                        all_features.append({"centroid": (pt[0], pt[1]),
                                             "properties": {field: fmt, "raw": raw}})
                        count += 1
                    except Exception: continue
            except Exception: continue
        Logger.ok("  MMS: " + str(count) + " points from '" + key + "' (plot " + plot_id + ")")
    if all_features:
        spatial.register(layer, all_features)
        Logger.ok("Registered " + str(len(all_features)) + " MMS Block Numbering points total")

def match_layers_from_cli(all_layers, cli_args):
    """Match layer names/aliases from CLI tokens.

    Handles both quoted ("Electric Trenches") and unquoted (Electric Trenches)
    multi-word layer names by trying progressive token merges.
    """
    import difflib
    selected  = []
    tokens    = list(cli_args)
    i         = 0

    while i < len(tokens):
        matched    = None
        match_type = None
        match_len  = 1   # how many tokens consumed

        # Try merging increasing numbers of tokens (handles unquoted multi-word names)
        for span in range(len(tokens) - i, 0, -1):
            candidate = " ".join(tokens[i:i+span])
            cl = candidate.lower().strip()

            # 1. Exact name match
            for layer in all_layers:
                if cl == layer["name"].lower():
                    matched = layer; match_type = "exact"; match_len = span; break
            if matched: break

            # 2. Alias match
            for layer in all_layers:
                if cl in [a.lower() for a in layer.get("cli_aliases", [])]:
                    matched = layer; match_type = "alias"; match_len = span; break
            if matched: break

            # 3. Substring match (only for full candidate, not partial spans)
            if span == len(tokens) - i or span == 1:
                for layer in all_layers:
                    if cl in layer["name"].lower():
                        matched = layer; match_type = "substring"; match_len = span; break
                if matched: break

            # 4. Fuzzy match (single token or full candidate)
            if span == 1 or span == len(tokens) - i:
                best = 0.0; bl = None
                for layer in all_layers:
                    r = difflib.SequenceMatcher(None, cl, layer["name"].lower()).ratio()
                    if r > best: best = r; bl = layer
                if best >= 0.82:
                    matched = bl; match_type = f"fuzzy({int(best*100)}%)"; match_len = span; break

        if matched:
            if matched not in selected:
                selected.append(matched)
                consumed = " ".join(tokens[i:i+match_len])
                Logger.ok(f"[{match_type}] '{consumed}' -> '{matched['name']}'")
            else:
                Logger.info(f"[dup] '{matched['name']}' already selected")
            i += match_len
        else:
            Logger.err(f"No match for '{tokens[i]}'")
            i += 1

    return selected


def select_layers(layers):
    # Default: only unlocked layers are ON
    selected = [not lyr.get("locked", True) for lyr in layers]
    done     = False
    while not done:
        print(f"\n{'='*60}")
        print("  DWG → GeoJSON Executor — Layer Selector")
        print("  Toggle ON/OFF, then X to run  |  \U0001F512=locked  \U0001F513=unlocked")
        print(f"{'='*60}")
        for i, lyr in enumerate(layers):
            st    = "ON " if selected[i] else "OFF"
            _icon = "\U0001F512" if lyr.get("locked", True) else "\U0001F513"
            print(f"  [{i+1:2d}] [{st}] {_icon} {lyr['name']:<38}")
        print(f"{'─'*60}")
        print("  [A] All ON   [N] All OFF   [X] Run")
        print(f"{'='*60}")
        choice = input("Choice: ").strip().upper()
        if choice == "A":
            selected = [True] * len(layers)
        elif choice == "N":
            selected = [False] * len(layers)
        elif choice == "X":
            done = True
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(layers):
                    selected[idx] = not selected[idx]
            except ValueError:
                pass
    return [l for l, s in zip(layers, selected) if s]


# ============================================================
# MAIN
# ============================================================

def main():
    import time as _time
    parser = argparse.ArgumentParser(
        description="DWG to GeoJSON Executor",
        epilog="Config is auto-detected from the script folder — no need to type it.")
    parser.add_argument("config", nargs="?", default=None,
                        help="Path to YAML config (optional — auto-detected if omitted)")
    parser.add_argument("--layers", "-l", nargs="*")
    parser.add_argument("--list",        action="store_true",
                        help="List all layers with lock status and exit")
    parser.add_argument("--run",         nargs="?", const="all", metavar="all",
                        help="Run unlocked layers. Use --run all")
    parser.add_argument("--dwg-layers",  metavar="DWG_KEY",
                        help="Print all layer names in a DWG and exit")
    parser.add_argument("--unlock",      nargs="+", metavar="LAYER",
                        help="Unlock layer(s) by name or alias in the YAML, then exit")
    parser.add_argument("--lock",        nargs="+", metavar="LAYER",
                        help="Lock layer(s) by name or alias in the YAML, then exit")
    parser.add_argument("--unlock-all",  action="store_true",
                        help="Unlock ALL layers in the YAML, then exit")
    parser.add_argument("--lock-all",    action="store_true",
                        help="Lock ALL layers in the YAML, then exit")
    parser.add_argument("--status",      action="store_true",
                        help="Show lock status of all layers and exit")
    args = parser.parse_args()

    # ── Config auto-discovery ─────────────────────────────────────────────────
    # If no config argument given, look for a YAML next to this script file.
    # Search order:
    #   1. Argument provided — use it directly
    #   2. config.yaml next to this script
    #   3. Any single *.yaml file next to this script
    if not args.config:
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        _default    = os.path.join(_script_dir, "config.yaml")
        if os.path.exists(_default):
            args.config = _default
        else:
            import glob as _glob
            _yamls = _glob.glob(os.path.join(_script_dir, "*.yaml"))
            if len(_yamls) == 1:
                args.config = _yamls[0]
            elif len(_yamls) > 1:
                print(f"  Multiple YAML files found — specify one:")
                for _y in _yamls:
                    print(f"    {_y}")
                sys.exit(1)
            else:
                print(f"  No config YAML found next to script: {_script_dir}")
                print(f"  Pass it explicitly:  python executor.py path\\to\\config.yaml")
                sys.exit(1)
    _job_start = _time.time()
    Logger.section("DWG → GeoJSON Executor")

    cfg        = load_config(args.config)
    global_cfg = cfg.get("global", {})
    all_layers = cfg.get("layers", [])
    output_dir = global_cfg.get("output_dir", "./output")
    dwg_paths  = global_cfg.get("source_dwgs", {})

    Logger.ok(f"Config:  {args.config}")
    Logger.ok(f"Output:  {output_dir}")
    Logger.ok(f"Layers:  {len(all_layers)} defined")

    if args.list:
        print(f"\n  {'#':<4} {'Layer':<40} {'Status':<12}  Source")
        print(f"  {'─'*75}")
        for i, lyr in enumerate(all_layers):
            _locked = lyr.get("locked", True)
            _icon   = "\U0001F512 LOCKED  " if _locked else "\U0001F513 UNLOCKED"
            _src    = lyr.get("source_layer", "?") or "~derived~"
            print(f"  {i+1:<4} {lyr['name']:<40} {_icon}  {_src}")
        _nlocked   = sum(1 for l in all_layers if l.get("locked", True))
        _nunlocked = sum(1 for l in all_layers if not l.get("locked", True))
        print(f"\n  \U0001F512 Locked: {_nlocked}   \U0001F513 Unlocked: {_nunlocked}")
        return

    # ── Lock / Unlock helpers — rewrite YAML directly, no AutoCAD needed ─────
    def _yaml_set_lock(names_or_aliases, lock_value):
        """Match layers by name/alias and set locked: true/false in the YAML file."""
        with open(args.config, "r", encoding="utf-8") as _f:
            _txt = _f.read()
        _matched = match_layers_from_cli(all_layers, names_or_aliases)
        if not _matched:
            Logger.err("No matching layers found — YAML unchanged")
            return
        for _lyr in _matched:
            _n   = _lyr["name"]
            _new = "true" if lock_value else "false"
            _txt = re.sub(
                rf'(  - name:\s+"{re.escape(_n)}"[\s\S]*?\n)(    locked:\s+\w+)',
                rf'\g<1>    locked:       {_new}',
                _txt
            )
            _state = "\U0001F512 LOCKED  " if lock_value else "\U0001F513 UNLOCKED"
            Logger.ok(f"  {_state}  {_n}")
        with open(args.config, "w", encoding="utf-8") as _f:
            _f.write(_txt)
        Logger.ok(f"Saved → {args.config}")

    if getattr(args, "lock_all", False):
        Logger.section("Locking ALL layers")
        with open(args.config, "r", encoding="utf-8") as _f:
            _t = _f.read()
        _t = re.sub(r'    locked:\s+\w+', '    locked:       true', _t)
        with open(args.config, "w", encoding="utf-8") as _f:
            _f.write(_t)
        Logger.ok(f"All {len(all_layers)} layers locked \U0001F512")
        return

    if getattr(args, "unlock_all", False):
        Logger.section("Unlocking ALL layers")
        with open(args.config, "r", encoding="utf-8") as _f:
            _t = _f.read()
        _t = re.sub(r'    locked:\s+\w+', '    locked:       false', _t)
        with open(args.config, "w", encoding="utf-8") as _f:
            _f.write(_t)
        Logger.ok(f"All {len(all_layers)} layers unlocked \U0001F513")
        return

    if getattr(args, "lock", None):
        Logger.section("Locking layers")
        _yaml_set_lock(args.lock, True)
        return

    if getattr(args, "unlock", None):
        Logger.section("Unlocking layers")
        _yaml_set_lock(args.unlock, False)
        return

    if getattr(args, "status", False):
        print(f"\n  {'#':<4} {'Layer':<40} {'Status':<12}  Source")
        print(f"  {'─'*75}")
        for i, lyr in enumerate(all_layers):
            _locked = lyr.get("locked", True)
            _icon   = "\U0001F512 LOCKED  " if _locked else "\U0001F513 UNLOCKED"
            _src    = lyr.get("source_layer", "?") or "~derived~"
            print(f"  {i+1:<4} {lyr['name']:<40} {_icon}  {_src}")
        _nl = sum(1 for l in all_layers if l.get("locked", True))
        _nu = sum(1 for l in all_layers if not l.get("locked", True))
        print(f"\n  \U0001F512 Locked: {_nl}   \U0001F513 Unlocked: {_nu}")
        return

    if args.dwg_layers:
        # Diagnostic: connect to AutoCAD and dump all layer names in the given DWG key
        Logger.section("Connecting to AutoCAD")
        acad = AutoCADManager()
        _dk   = args.dwg_layers
        _path = dwg_paths.get(_dk)
        if not _path:
            Logger.err(f"DWG key '{_dk}' not found in config. "
                       f"Available keys: {list(dwg_paths.keys())}")
            return
        _msp = acad.get_modelspace(_path)
        if not _msp:
            Logger.err(f"Could not open DWG for key '{_dk}': {_path}")
            return
        _layers = sorted(get_all_layers(_msp))
        print(f"\n  DWG key : {_dk}")
        print(f"  Path    : {_path}")
        print(f"  Layers  : {len(_layers)}\n")
        for _ln in _layers:
            # Show entity type counts alongside each layer name
            _midx  = get_msp_index(_msp)
            _types = _midx.get('__types__', {}).get(_ln, {})
            _total = sum(_types.values())
            print(f"  {_ln:<55}  ({_total:>5} entities: {_types})")
        return

    # Connect
    Logger.section("Connecting to AutoCAD")
    acad = AutoCADManager()
    for d in acad.list_open_docs():
        Logger.info(f"  Open: {d}")

    # Select
    if args.run == "all":
        selected = [l for l in all_layers if not l.get("locked", True)]
        if not selected:
            Logger.err("--all: all layers are locked. Use --unlock <name> or --unlock-all first.")
            return
        Logger.ok(f"--run all: {len(selected)} unlocked layer(s) selected")
    elif args.run and args.run != "all":
        Logger.err(f'Unknown --run value "{args.run}". Did you mean --run all?')
        return
    elif args.layers:
        selected = match_layers_from_cli(all_layers, args.layers)
        Logger.ok(f"Selected {len(selected)} layers from CLI")
    else:
        selected = select_layers(all_layers)

    # Warn and strip any locked layers from the selection.
    # Exception: layers with derive_from set are always allowed through — they do
    # not extract from DWG, so locked:true does not apply to them.
    _locked_sel   = [l for l in selected if     l.get("locked", True) and not l.get("derive_from")]
    _unlocked_sel = [l for l in selected if not l.get("locked", True) or  l.get("derive_from")]
    if _locked_sel:
        Logger.warn("The following layers are LOCKED and will be skipped:")
        for _ll in _locked_sel:
            Logger.warn(f"    \U0001F512  {_ll['name']}  — run: gsa config --unlock \"{_ll['name']}\"")
    selected = _unlocked_sel

    if not selected:
        Logger.err("No unlocked layers to run. Use --unlock <layer> or --status to check.")
        return

    # Init
    plot_registry = PlotRegistry()
    spatial       = SpatialJoinEngine()
    fe            = FieldEngine(global_cfg, plot_registry)
    extractor     = LayerExtractor(acad, fe, spatial, global_cfg, dwg_paths)
    report        = []

    # Pass 1 — Spatial Join Sources
    Logger.section("Pass 1 — Spatial Join Sources")
    # Find the spatial reference layer (role: spatial_reference in YAML)
    _bb_cfg_check  = next((l for l in cfg["layers"]
                           if l.get("role") == "spatial_reference"), None)
    _bb_json_check = os.path.join(output_dir,
                         _bb_cfg_check.get("output", "reference_layer.geojson")
                     ) if _bb_cfg_check else None
    _bb_on_disk    = bool(_bb_json_check and os.path.exists(_bb_json_check))

    if _bb_on_disk:
        Logger.ok("Spatial reference layer found on disk — skipping layout scan")
    else:
        register_mms_block_numbering(
            acad, global_cfg, spatial, dwg_paths, plot_registry)

    # Spatial reference layer — load/extract for spatial join cache.
    # Only written to disk if explicitly selected by the user.
    bb_cfg      = next((l for l in cfg["layers"]
                        if l.get("role") == "spatial_reference"), None)
    bb_feats    = []
    _ref_name   = bb_cfg["name"] if bb_cfg else ""
    bb_geojson  = os.path.join(output_dir,
                      bb_cfg.get("output", "reference_layer.geojson")) if bb_cfg else None
    _bb_selected = bb_cfg in selected

    if bb_cfg:
        if _bb_on_disk and bb_geojson:
            Logger.ok(f"{_ref_name}: loading from disk for spatial join")
            import json as _json
            with open(bb_geojson, encoding="utf-8") as _f:
                _fc = _json.load(_f)
            bb_feats = _fc.get("features", [])
            for feat in bb_feats:
                coords = feat.get("geometry", {}).get("coordinates", [[]])[0]
                if coords:
                    feat["_centroid"] = (
                        sum(c[0] for c in coords) / len(coords),
                        sum(c[1] for c in coords) / len(coords))
                    # Store polygon ring so point-in-polygon tests can use
                    # exact containment instead of nearest-centroid fallback
                    feat["_pts"] = [(c[0], c[1]) for c in coords]
            Logger.ok(f"Loaded {len(bb_feats)} {_ref_name} features from disk")
        else:
            Logger.info(f"{_ref_name}: extracting from DWG for spatial join cache...")
            bb_feats = extractor.extract(bb_cfg) or []
            if _bb_selected and bb_geojson and bb_feats:
                import json as _json
                os.makedirs(output_dir, exist_ok=True)
                clean = [{k: v for k, v in f.items() if k != "_centroid"} for f in bb_feats]
                _crs_bb = global_cfg.get("crs", "EPSG:32642")
                _out_stem = os.path.splitext(os.path.basename(bb_geojson))[0]
                with open(bb_geojson, "w", encoding="utf-8") as _f:
                    _json.dump({
                        "type": "FeatureCollection",
                        "name": _out_stem,
                        "crs": {"type": "name", "properties": {
                            "name": f"urn:ogc:def:crs:EPSG::{_crs_bb.split(':')[-1]}"
                        }},
                        "features": clean
                    }, _f, ensure_ascii=False, indent=2)
                Logger.ok(f"Saved {_ref_name} -> " + bb_geojson)
            elif not _bb_selected:
                Logger.info(f"{_ref_name} extracted for spatial join only — not written (not in selection)")

        spatial.register(
            _ref_name,
            [{"centroid":  f.get("_centroid", (0, 0)),
              "polygon":   f.get("_pts"),          # polygon ring for PIP containment
              "properties": f["properties"]}
             for f in bb_feats if f.get("_centroid")])
        spatial.spatial_ref_key = _ref_name

    # Load Section Mark MTEXT into spatial cache for trench attachment splitting
    # Scans both geometry_params.section_mark_layer (old polygon path) and
    # fields.*.conditional.then.section_layer (new line path for HT trenches)
    _sm_needed = set()
    for _lcfg in cfg["layers"]:
        _sml = _lcfg.get("geometry_params", {}).get("section_mark_layer")
        if _sml:
            _sm_needed.add(_sml)
        # Also scan fields for conditional section_layer (line geometry path)
        for _fn, _fc in _lcfg.get("fields", {}).items():
            if isinstance(_fc, dict):
                _cond = _fc.get("conditional", {})
                if isinstance(_cond.get("then"), dict):
                    _sml2 = _cond["then"].get("section_layer")
                    if _sml2:
                        _sm_needed.add(_sml2)
                    for _fb in _cond["then"].get("fallbacks", []):
                        _sm_needed.add(_fb)
    for _sml in _sm_needed:
        _sm_all = []
        for _dk, _dp in cfg["global"]["source_dwgs"].items():
            if not _dp: continue
            try:
                import win32com.client as _w32c
                _ax = _w32c.Dispatch("AutoCAD.Application")
                _dx = None
                for _di in range(_ax.Documents.Count):
                    _dd = _ax.Documents.Item(_di)
                    if os.path.normcase(os.path.basename(str(_dd.FullName))) ==                        os.path.normcase(os.path.basename(str(_dp))):
                        _dx = _dd; break
                if not _dx: continue
                for _ent in _dx.ModelSpace:
                    try:
                        if _ent.Layer != _sml: continue
                        if "MTEXT" not in _ent.EntityName.upper() and                            "TEXT"  not in _ent.EntityName.upper(): continue
                        _txt = EntityReader.strip_mtext(_ent.TextString)
                        if not _txt: continue
                        _ip = _ent.InsertionPoint
                        _sm_all.append({"centroid":(_ip[0],_ip[1]),"properties":{"label":_txt}})
                    except: continue
            except: continue
        if _sm_all:
            spatial.register(_sml, _sm_all)
            Logger.ok(f"Loaded {len(_sm_all)} section marks: '{_sml}'")

    # Auto-load previously saved geojsons — but ONLY layers that are
    # referenced as spatial join sources by the currently selected layers.
    # Loading every geojson on disk regardless of need wastes time and
    # pollutes the spatial cache with data the run never uses.
    import json as _json2

    # Collect every from_layer value the selected layers actually need
    _needed_for_joins = set()
    for _sl in selected:
        for _fn, _fc in _sl.get("fields", {}).items():
            if not isinstance(_fc, dict):
                continue
            _jt = _fc.get("spatial_join")
            if isinstance(_jt, dict):
                _fl = _jt.get("from_layer", "")
                if _fl:
                    _needed_for_joins.add(_fl)
            elif _jt in ("primary", "secondary"):
                # primary → MMS Block Numbering (already handled above)
                # secondary → spatial reference layer (already registered above)
                pass
    # Also build stem→name map for reverse lookup
    _stem_to_name = {}
    for _lcfg in cfg["layers"]:
        _lo = _lcfg.get("output", "")
        if _lo:
            _stem_to_name[os.path.splitext(_lo)[0].upper()] = _lcfg.get("name", "")

    for _lcfg in cfg["layers"]:
        _lname = _lcfg.get("name", "")
        _lout  = _lcfg.get("output", "")
        if not _lname or not _lout or _lcfg.get("role") == "spatial_reference":
            continue
        # Skip unless this layer is a join source for someone in the selection
        _stem = os.path.splitext(_lout)[0].upper()
        if _lname not in _needed_for_joins and _stem not in _needed_for_joins:
            continue
        _lpath = os.path.join(output_dir, _lout)
        if not os.path.exists(_lpath):
            continue
        try:
            with open(_lpath, encoding="utf-8") as _lf:
                _lfc = _json2.load(_lf)
            _lfeats = []
            for _lfeat in _lfc.get("features", []):
                _gtype   = _lfeat.get("geometry", {}).get("type", "")
                _lcoords = _lfeat.get("geometry", {}).get("coordinates", [])
                if _gtype == "Polygon" and _lcoords:
                    _pts = _lcoords[0]
                    if _pts:
                        # Bbox midpoint — faster than summing all vertices,
                        # accurate enough for nearest-centroid spatial joins
                        _xs = [p[0] for p in _pts]
                        _ys = [p[1] for p in _pts]
                        _lfeat["_centroid"] = (
                            (min(_xs) + max(_xs)) * 0.5,
                            (min(_ys) + max(_ys)) * 0.5)
                elif _gtype == "LineString" and _lcoords:
                    # Midpoint of first and last vertex — fast for long cables
                    _lfeat["_centroid"] = (
                        (_lcoords[0][0] + _lcoords[-1][0]) * 0.5,
                        (_lcoords[0][1] + _lcoords[-1][1]) * 0.5)
                elif _gtype == "Point" and _lcoords:
                    _lfeat["_centroid"] = tuple(_lcoords[:2])
                if "_centroid" in _lfeat:
                    _lfeats.append({"centroid": _lfeat["_centroid"],
                                    "properties": _lfeat.get("properties", {})})
            if _lfeats:
                spatial.register(_lname, _lfeats)
                Logger.ok(f"Pre-loaded {len(_lfeats)} features for join: '{_lname}'")
                # Also register under output filename stem
                # so from_layer can use either the full layer name or the stem
                if _stem and _stem != _lname.upper():
                    spatial.register(_stem, _lfeats)
                    Logger.ok(f"  also registered as '{_stem}' (stem alias)")
        except Exception:
            pass

    # Auto-extract any spatial join sources not yet registered and not on disk
    _registered = set(spatial.cache.keys()) if hasattr(spatial, "cache") else set()
    _needed_join_layers = set()
    for _sl in selected:
        for _fn, _fc in _sl.get("fields", {}).items():
            if isinstance(_fc, dict):
                _jt = _fc.get("spatial_join")
                if isinstance(_jt, dict):
                    _fl = _jt.get("from_layer", "")
                    if _fl and _fl not in _registered:
                        _needed_join_layers.add(_fl)
    # Build lookup: output filename stem (uppercase) → layer cfg
    _stem_to_cfg = {}
    for _lc in cfg["layers"]:
        _lo = _lc.get("output", "")
        if _lo:
            _s = os.path.splitext(_lo)[0].upper()
            _stem_to_cfg[_s] = _lc

    for _jlname in _needed_join_layers:
        # Try exact name match first, then stem match
        _jl_cfg = next((l for l in cfg["layers"] if l["name"] == _jlname), None)
        if not _jl_cfg:
            _jl_cfg = _stem_to_cfg.get(_jlname.upper())
        if not _jl_cfg:
            Logger.warn(f"Join source '{_jlname}' not found in config — spatial join may be empty")
            continue
        # LOCK: never auto-extract a locked layer — use its pre-loaded geojson cache only
        if _jl_cfg.get("locked", True):
            Logger.info(f"Join source '{_jlname}' is locked — using pre-loaded cache only")
            continue
        Logger.info(f"Auto-extracting join source '{_jlname}' (needed for spatial join)...")
        try:
            _jl_feats = extractor.extract(_jl_cfg) or []
            if _jl_feats:
                _jl_cache = [{"centroid":   f["_centroid"],
                               "insert_pt":  f.get("_insert_pt"),
                               "properties": f["properties"]}
                              for f in _jl_feats if f.get("_centroid")]
                spatial.register(_jlname, _jl_cache)   # register under the from_layer key used in config
                if _jl_cfg["name"] != _jlname:
                    spatial.register(_jl_cfg["name"], _jl_cache)  # also under full name
                Logger.ok(f"Registered {len(_jl_feats)} features for join source '{_jlname}'")
            else:
                Logger.warn(f"No features found for join source '{_jlname}'")
        except Exception as _je:
            Logger.warn(f"Could not extract join source '{_jlname}': {_je}")

    # Pass 2 — selected layers (spatial reference layer always first if in selection)
    Logger.section("Pass 2 — Extracting Selected Layers")

    bb_in_sel  = [l for l in selected if l.get("role") == "spatial_reference"]
    other_sel  = [l for l in selected if l.get("role") != "spatial_reference"]
    ordered    = bb_in_sel + other_sel

    # The set of layer names the user explicitly asked to run this session.
    # Every lock check uses this — only these layers may be extracted, written,
    # or have their cache entries replaced. All other layers are read-only.
    _target_names = {l["name"] for l in ordered}
    Logger.info(f"  Target layers this run: {sorted(_target_names)}")

    total_layers   = len(ordered)
    _layer_times   = []   # track elapsed time per completed layer for ETA
    _overall_start = _time.time()

    Logger.start_live_progress(total_layers, _overall_start)

    for _layer_idx, layer_cfg in enumerate(ordered):
        name        = layer_cfg["name"]
        _layer_start = _time.time()

        # Update overall layer bar
        _avg = sum(_layer_times) / len(_layer_times) if _layer_times else 0.0
        Logger.update_layer_progress(_layer_idx, total_layers, _avg)

        if layer_cfg.get("role") == "spatial_reference" and bb_feats:
            features = bb_feats

        elif layer_cfg.get("derive_from") == "spatial_reference":
            # ── DERIVED ZONE BOUNDARY: dissolved from spatial reference layer ──
            # No DWG extraction needed — shapely unary_union does it all.
            # bb_feats must be available (loaded or extracted in Pass 1).
            features = derive_zone_boundary_from_reference(
                bb_feats, layer_cfg, output_dir, crs=global_cfg.get("crs","EPSG:32642"))
            if not features:
                report.append((name, 0, "⚠  Derive failed — check spatial reference layer"))
                _layer_times.append(_time.time() - _layer_start)
                continue

        elif layer_cfg.get("locked", True):
            # ── LOCKED LAYER ─────────────────────────────────────────────────
            # locked: true  → layer is protected, executor will not touch it.
            # locked: false → layer is unlocked and will be extracted this run.
            # To run a layer: set locked: false in the YAML (or use --unlock).
            # To protect a layer again: set locked: true (or use --lock).
            Logger.info(f"  LOCKED  '{name}' — skipping")
            report.append((name, 0, "\U0001F512  Locked"))
            _layer_times.append(_time.time() - _layer_start)
            continue

        elif layer_cfg.get("derive_from") == "parent_layer":
            # ── Derived layer: positions inherited from parent layer ───────────
            # ALWAYS re-extract parent layer live — _attr_pts (per-attribute world
            # positions) are not stored in GeoJSON or cache, so disk/cache paths
            # would give only centroid and all points would stack on one location.
            features = []
            _parent_layer_name = layer_cfg.get("parent_layer_name", "")
            _st_cfg  = next((l for l in cfg["layers"]
                              if l["name"] == _parent_layer_name), None)
            _st_live = []
            if _st_cfg:
                Logger.step("Derived layer: re-extracting parent layer for per-attribute positions")
                _st_live = extractor.extract(_st_cfg) or []
                Logger.ok(f"  {len(_st_live)} parent features extracted")
            else:
                Logger.warn("Derived layer: parent_layer_name not found in config")

            _code   = layer_cfg.get("code", "")
            _seq    = 0
            _id_map = [("ID_01","ID1"), ("ID_02","ID2"),
                       ("ID_03","ID3"), ("ID_04","ID4")]

            for _st in _st_live:
                _pts_poly = _st.get("_pts", [])
                _ct       = _st.get("_centroid", (0.0, 0.0))
                _sprops   = _st.get("properties", {})
                _plot_no  = _sprops.get("Plot_No", "")
                _pnum     = int(re.findall(r'\d+', str(_plot_no))[0]) \
                            if re.findall(r'\d+', str(_plot_no)) else 0
                _clean_code = _code

                # ── Centreline spacing ───────────────────────────────────────
                # Find parent feature long axis from its bounding box.
                # Parent features are typically elongated rectangles in UTM.
                # Place one point per child, evenly spaced along the long axis,
                # all on the centreline (midpoint of short axis).
                # For n children: positions at i/(n+1) fractions along the long axis.
                _filled_ids = [(idf, tag) for idf, tag in _id_map
                               if _sprops.get(idf, "").strip() not in ("", " ")]
                _n = len(_filled_ids)

                if _pts_poly and len(_pts_poly) >= 4 and _n > 0:
                    _xs = [p[0] for p in _pts_poly[:-1]]  # exclude closing vertex
                    _ys = [p[1] for p in _pts_poly[:-1]]
                    _xmin, _xmax = min(_xs), max(_xs)
                    _ymin, _ymax = min(_ys), max(_ys)
                    _cx = (_xmin + _xmax) / 2.0   # centreline X (mid of short axis)
                    _cy = (_ymin + _ymax) / 2.0   # centreline Y (mid of long axis)
                    _dx = _xmax - _xmin
                    _dy = _ymax - _ymin

                    if _dy >= _dx:
                        # Long axis = Y (portrait orientation)
                        _string_pts = [
                            (_cx, _ymin + (i + 1) / (_n + 1) * _dy)
                            for i in range(_n)
                        ]
                    else:
                        # Long axis = X (landscape orientation)
                        _string_pts = [
                            (_xmin + (i + 1) / (_n + 1) * _dx, _cy)
                            for i in range(_n)
                        ]
                else:
                    # No polygon — all points at centroid (shouldn't happen)
                    _string_pts = [_ct] * _n

                for _idx, (_idf, _tag) in enumerate(_filled_ids):
                    _sid = _sprops.get(_idf, "")
                    if not _sid or _sid.strip() in ("", " "):
                        continue
                    _seq += 1
                    # Use the full sub-plot ID (e.g. 'A9a', 'A9b') so
                    # Connection_ID is consistent with Plot_No — same logic
                    # as FieldEngine.resolve().  Fall back to A{pnum:02d}
                    # if Plot_No is somehow absent or blank.
                    _conn_plot = (
                        _plot_no if (_plot_no and _plot_no.strip() not in ("", " "))
                        else f"A{_pnum:02d}"
                    )
                    _conn_id = f"{_conn_plot}_{_clean_code}_{_seq:02d}"

                    # Point at this child's position along the parent feature centreline
                    _tp = _string_pts[_idx]

                    _props = {
                        "OBJECTID":              _seq,
                        "Connection_ID":         _conn_id,
                        "Plant_Name":            _sprops.get("Plant_Name", ""),
                        "Code":                  layer_cfg.get("code", ""),
                        "Category":              layer_cfg.get("category", ""),
                        "Classification":        layer_cfg.get("classification", ""),
                        "Sub_Classification":    layer_cfg.get("sub_classification", ""),
                        "Plot_No":               _plot_no,
                        "Block_No":              _sprops.get("Block_No", " "),
                        "Modules_per_String":    28,
                        "String_Code":           _sid,
                        "String_Capacity":       0,
                        "Make":                  " ",
                        "Length_mm":             " ",
                        "Width_mm":              " ",
                        "Depth_mm":              " ",
                        "Module_Type":           " ",
                        "Type_Cell":             " ",
                        "Module_Details":        " ",
                        "Pmax_STC":              " ",
                        "Max_Voltage":           " ",
                        "Max_Current":           " ",
                        "Open_Circuit_Voltage":  " ",
                        "Short_Circuit_Current_A": " ",
                        "Efficiency_STC":        " ",
                        "Level_Grade":           " ",
                        "Development_Status":    " ",
                        "Development_Year":      " ",
                        "Operational_Status":    " ",
                        "Operational_Year":      " ",
                        "Owned_By":              " ",
                        "Developed_By":          " ",
                        "Maintained_By":         " ",
                        "Prepared_By":           " ",
                        "Country":               _sprops.get("Country", ""),
                        "State":                 _sprops.get("State", ""),
                        "District":              _sprops.get("District", ""),
                        "Taluka":                _sprops.get("Taluka", ""),
                        "Village":               _sprops.get("Village", ""),
                        "Jurisdiction":          _sprops.get("Jurisdiction", ""),
                        "Attachment":            " ",
                    }
                    features.append({
                        "type":      "Feature",
                        "geometry":  {"type": "Point",
                                      "coordinates": [_tp[0], _tp[1]]},
                        "properties": _props,
                        "_centroid":  _tp,
                        "_pts":       [_tp],
                    })

            Logger.ok(f"Derived {len(features)} child features "
                      f"from {len(_st_live)} parent features")

        else:
            features = extractor.extract(layer_cfg)

        _layer_times.append(_time.time() - _layer_start)

        if not features:
            report.append((name, 0, "\u26a0  No features"))
            continue

        # ── Post-extraction: exclusive 1-to-1 spatial assignment ──────────────
        # For any field using method: nearest_exclusive, the per-feature nearest
        # join during extraction is skipped (field left as placeholder " ").
        # Here we do the proper greedy 1-to-1 assignment across all features at
        # once, then patch them in-place before writing.
        # Features carry their geometry as "_pts" set by extractor (added below).
        for _pfn, _pfc in layer_cfg.get("fields", {}).items():
            if not isinstance(_pfc, dict):
                continue
            _pjt = _pfc.get("spatial_join")
            if not isinstance(_pjt, dict):
                continue
            if _pjt.get("method") != "nearest_exclusive":
                continue
            _p_lyr  = _pjt.get("from_layer", "")
            _p_fld  = _pjt.get("from_field", "")
            _p_tfm  = _pjt.get("transform", {})
            _p_fmt  = _p_tfm.get("format", "") if isinstance(_p_tfm, dict) else ""
            Logger.step(f"Post-assign exclusive: '{_pfn}' ← '{_p_lyr}'.'{_p_fld}'")
            spatial.assign_exclusive(
                features,
                layer_name   = _p_lyr,
                field        = _p_fld,
                pts_key      = "_pts",
                result_field = _pfn,
                transform_fmt= _p_fmt,
                plot_field   = "Plot_No",
            )

        # Register for downstream spatial joins — only if this is a target layer.
        # Locked layers are pre-loaded from disk; we must not overwrite that cache
        # entry with a freshly extracted version (which may differ or be incomplete).
        if name in _target_names:
            spatial.register(
                name,
                [{"centroid":    f["_centroid"],
                  "insert_pt":   f.get("_insert_pt"),
                  "attr_pts":    f.get("_attr_pts", {}),
                  "properties":  f["properties"]}
                 for f in features])

        out = os.path.join(
            output_dir,
            layer_cfg.get("output", f"{name.replace(' ','_')}.geojson"))
        _crs = global_cfg.get("crs", "EPSG:32642")
        # LOCK: final safety net — never write a locked layer that slipped through
        if layer_cfg.get("locked", True) and name not in _target_names:
            Logger.warn(f"LOCK: refusing to write locked layer '{name}' — not a target")
            report.append((name, len(features), "\u26a0  Blocked — locked"))
        else:
            write_geojson(features, out, crs=_crs)
            report.append((name, len(features), "\u2713  OK"))

    # Report
    Logger.update_layer_progress(total_layers, total_layers, 0.0)
    time.sleep(0.3)   # let the thread render 100% once
    Logger.stop_live_progress()
    Logger.section("Export Report")
    print(f"  {'Layer':<40} {'Features':>10}  Status")
    print(f"  {'─'*58}")
    for name, count, status in report:
        count_str = f"{count:>10}" if isinstance(count, int) and "\U0001F512" not in status else f"{'—':>10}"
        print(f"  {name:<40} {count_str}  {status}")
    _job_elapsed = _time.time() - _job_start
    if _job_elapsed >= 60:
        _job_time = f"{int(_job_elapsed//60)}m {int(_job_elapsed%60)}s"
    else:
        _job_time = f"{_job_elapsed:.1f}s"
    bar_done = "\u2588" * 50
    print(f"\n  Overall [{bar_done}] 100%  {total_layers}/{total_layers}  complete")
    print(f"  Output: {output_dir}")
    print(f"  Total time: {_job_time}")
    print(f"  Done.\n")


if __name__ == "__main__":
    main()