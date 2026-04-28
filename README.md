# DWG → GeoJSON Executor

A general-purpose tool that converts AutoCAD DWG layers to GeoJSON. It connects directly to a running AutoCAD instance over COM — no manual export or intermediate format needed. Every aspect of the conversion (geometry type, field schema, field order, spatial joins, calculated values, zone assignment, locking) is driven entirely by a YAML configuration file. No code changes are required between projects.

---

## What This Tool Is For

Any project where AutoCAD drawings need to be converted to GeoJSON for use in GIS, web maps, spatial databases, or data pipelines. Common use cases include:

- **Infrastructure mapping** — extracting asset inventories (structures, installations, networks) from site layouts
- **Civil and surveying** — converting site parcels, boundaries, corridors, and alignments into spatial datasets
- **Utilities** — mapping conduit routes, cable paths, service areas from engineering drawings
- **Construction management** — extracting zone boundaries, equipment positions, and access routes from site DWGs
- **Urban and land use** — converting building footprints, plot boundaries, and land-use polygons from architectural drawings
- **Environmental and field survey** — converting survey grids, sample areas, and observation points from CAD base maps
- **Facilities and asset management** — extracting georeferenced asset databases from as-built drawings

If your project has AutoCAD drawings with named layers, and those layers contain geometry with attributes, this tool converts them to GeoJSON with a custom field schema that you define.

---

## How It Works — Overview

```
AutoCAD (open, COM)
        │
        │  reads entities directly from model space
        ▼
  executor.py  ←── config.yaml  (your field schema, layer list, DWG paths)
        │
        │  resolves fields, performs spatial joins, applies filters
        ▼
  output/*.geojson  (one file per layer, CRS defined by you)
```

The executor reads AutoCAD entity geometry (LWPOLYLINE, INSERT, LINE, POINT, etc.) directly from the live AutoCAD COM interface — no export step required. For layers populated with symbol blocks (INSERT entities), it opens each block instance and extracts the attribute tags you specify in your config, mapping them to whatever output field names you choose. It then runs your field resolution rules (constants, attribute lookups, spatial joins, calculations, derivations) to build the full property schema for each feature, and writes standard GeoJSON.

The config file is the only thing that changes between projects. The executor code is not modified.

### Key Capabilities

| Capability | Description |
|---|---|
| **DWG attribute → GeoJSON property** | For any DWG layer, you list the attribute tags you want in your YAML config. The executor reads those tag values from every entity on that layer and writes them as properties in the output GeoJSON — under whatever field names you choose. No hardcoded schema; you define the field list, the field names, and the field order. |
| **Block INSERT extraction** | Layers made up of symbol blocks (INSERT entities) are fully supported. The executor opens each block instance, reads its named attribute tags, and maps them to your declared output fields. |
| **Constants and computed fields** | Mix live DWG attribute values with constants, sequential IDs, spatial joins, and calculated expressions in the same feature's properties. |
| **Spatial attribute enrichment** | Features can inherit attribute values from spatially overlapping or nearest features on other layers (e.g. a point inheriting its enclosing zone's ID). |
| **Geometry and attributes together** | Every output feature carries both the correct geometry (point, line, or polygon, in the CRS you specify) and the full attribute schema you declared — ready for direct import into QGIS, PostGIS, Mapbox, or any GeoJSON-aware tool. |

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Installation](#2-installation)
3. [AutoCAD Setup](#3-autocad-setup)
4. [Quick Start](#4-quick-start)
5. [Execution Model — Two Passes](#5-execution-model--two-passes)
6. [Config File Structure](#6-config-file-structure)
7. [Global Section](#7-global-section)
8. [The Zone / Spatial Reference System](#8-the-zone--spatial-reference-system)
9. [Zone ID Auto-Detection from DWG Filenames](#9-zone-id-auto-detection-from-dwg-filenames)
10. [Layer Types](#10-layer-types)
    - [Type 1 — Spatial Reference Layer](#type-1--spatial-reference-layer)
    - [Type 2 — Derived Zone Boundary](#type-2--derived-zone-boundary)
    - [Type 3 — Standard Layer (point / line / polygon)](#type-3--standard-layer-point--line--polygon)
    - [Type 4 — Derived Child Layer](#type-4--derived-child-layer)
11. [Field Reference](#11-field-reference)
12. [Geometry Params Reference](#12-geometry-params-reference)
13. [Spatial Join Reference](#13-spatial-join-reference)
14. [Derive Transforms Reference](#14-derive-transforms-reference)
15. [The Connection_ID System](#15-the-connection_id-system)
16. [Multi-DWG Projects](#16-multi-dwg-projects)
17. [Working with Block Attributes](#17-working-with-block-attributes)
18. [CLI Reference](#18-cli-reference)
19. [Lock System](#19-lock-system)
20. [Output Format](#20-output-format)
21. [Config Management and Version Control](#21-config-management-and-version-control)
22. [Performance Notes](#22-performance-notes)
23. [Troubleshooting](#23-troubleshooting)
24. [Project Setup Checklist](#24-project-setup-checklist)

---

## 1. Requirements

| Item | Minimum | Notes |
|---|---|---|
| Operating System | Windows 10 | The COM bridge to AutoCAD is Windows-only |
| AutoCAD | Any version with a COM API | Must be running with DWGs open before the executor starts |
| Python | 3.8+ | Standard CPython — no special distribution needed |
| pywin32 | any recent | `pip install pywin32` — provides the COM interface |
| pyyaml | any recent | `pip install pyyaml` — reads the config file |
| shapely | 1.8+ | `pip install shapely` — only required for zone boundary dissolve layers |

---

## 2. Installation

```
pip install pywin32 pyyaml shapely
```

No other setup. The executor is a single self-contained Python file (`executor.py`). Place it in your project folder alongside your config YAML. No package installation, no virtual environment required (though using one is good practice).

---

## 3. AutoCAD Setup

The executor reads geometry and attributes directly from AutoCAD's model space over COM. This means:

- AutoCAD must be running and fully loaded before you run the executor
- Every DWG file listed in `source_dwgs` in your config must be open as a document in that AutoCAD session
- The DWG files must be in model space (layouts/paper space are not read)
- Do not close AutoCAD, switch documents, or trigger a DWG regeneration while the executor is running

**Document matching:** The executor finds open documents by comparing the full file path in your config against open document paths in AutoCAD. If the full path does not match (e.g. a mapped drive vs UNC path), it falls back to matching by filename. If a DWG in your config is not open, that layer is skipped with a warning and extraction continues with the remaining layers.

**Multiple DWGs:** All DWGs can be open simultaneously in the same AutoCAD session. The executor switches between them programmatically — you do not need to manually activate each one.

---

## 4. Quick Start

```
# Step 1: Copy and rename the sample config
copy tora_config_sample.yaml config.yaml

# Step 2: Open AutoCAD and load your DWG files

# Step 3: Find your exact AutoCAD layer names
python executor.py config.yaml --dwg-layers layout_a

# Step 4: Edit config.yaml — fill in DWG paths, layer names, fields

# Step 5: Check the layer list and lock status
python executor.py config.yaml --list

# Step 6: Unlock the first layer you want to extract
python executor.py config.yaml --unlock "My Layer Name"

# Step 7: Run
python executor.py config.yaml --run

# Step 8: Verify output, then lock the finished layer
python executor.py config.yaml --lock "My Layer Name"
```

If your config is named `config.yaml` and sits next to `executor.py`, the path argument is optional:

```
python executor.py --run
```

**Config auto-discovery** (when no path is given):
1. `config.yaml` in the same folder as `executor.py`
2. Any single `*.yaml` file in the same folder (error printed if multiple are found)

---

## 5. Execution Model — Two Passes

Understanding the two-pass model helps you configure layer ordering and avoid spatial join failures.

### Pass 1 — Build the Spatial Cache

Before any output is written, the executor builds up its spatial reference data:

**1a. Spatial reference layer**

The layer with `role: spatial_reference` is loaded first. Its polygons define the zone grid used by all `spatial_join: secondary` lookups throughout the run.

- If the spatial reference GeoJSON already exists on disk (from a prior run), it is loaded from the file. AutoCAD is not read. This is the fast path for runs where the zone grid is stable.
- If the GeoJSON does not exist, it is extracted from AutoCAD and (if selected for output) written to disk before pass 2 begins.

**1b. Spatial join sources**

Any layer referenced by a `spatial_join: { from_layer: "..." }` field in any other layer's config is extracted in pass 1 and registered in the spatial cache. This ensures the join source is available when the dependent layer runs in pass 2.

### Pass 2 — Extract Selected Layers

Layers run in YAML config order, with the spatial reference layer promoted to first position if it is in the current selection. For each layer:

1. Entities are read from the DWG model space
2. Geometry is extracted (polygon ring, polyline vertices, insert point)
3. All `fields:` are resolved in two sub-passes (constants/joins first, derivations second)
4. Features are written to a GeoJSON file in `output_dir`
5. The layer's features are registered in the spatial cache so subsequent layers can join against it

**Implication for layer ordering:** if layer B uses `spatial_join: { from_layer: "Layer A" }`, then Layer A must appear earlier in the YAML config than Layer B, or Layer A must already be registered from pass 1 (spatial reference or spatial join source).

---

## 6. Config File Structure

A config file has two top-level keys:

```yaml
global:
  # project-wide settings: paths, CRS, common fields, zone grid config
  ...

layers:
  # list of layers to extract — one entry per output GeoJSON
  - name: "Zone Grid"
    role: spatial_reference
    ...

  - name: "Asset Layer"
    geometry: point
    ...

  - name: "Route Layer"
    geometry: line
    ...

  - name: "Area Layer"
    geometry: polygon
    ...
```

Each entry under `layers:` produces one `.geojson` output file. The YAML list order controls the execution order.

---

## 7. Global Section

```yaml
global:

  # Metadata — used for documentation; not written to features directly
  project_name: "Project Name"
  company:      "Organisation Name"
  site_name:    "Site or Facility Name"

  # Coordinate reference system for all output GeoJSON files.
  # Use the EPSG code for the UTM zone covering your project area.
  # Common examples — pick the zone for your location:
  #   EPSG:32629  → UTM zone 29N  (western Europe, Atlantic)
  #   EPSG:32632  → UTM zone 32N  (central Europe)
  #   EPSG:32637  → UTM zone 37N  (East Africa, Middle East)
  #   EPSG:32644  → UTM zone 44N  (central/south Asia)
  #   EPSG:32754  → UTM zone 54S  (eastern Australia)
  #   EPSG:32718  → UTM zone 18S  (South America, Andes)
  # Find your UTM zone: https://spatialreference.org or use QGIS / ArcGIS
  crs: "EPSG:32632"

  # Output folder — created automatically if it does not exist.
  # Relative paths are resolved from the script's working directory.
  output_dir: "./output"

  # DWG file aliases.
  # Keys are short names you reference in layer configs via source_dwg:.
  # Values are absolute paths to the DWG files.
  # All listed files must be open in AutoCAD before running.
  source_dwgs:
    drawing_a:  "C:/Project/DWGs/Drawing_A.dwg"
    drawing_b:  "C:/Project/DWGs/Drawing_B.dwg"
    routes:     "C:/Project/DWGs/Routes.dwg"

  # Zone grid configuration.
  # "block_no" is the fixed internal YAML key the executor reads — do not rename it.
  # It tells the executor which layer is the spatial reference and which field
  # in that layer holds the zone identifier assigned to features.
  # primary_source:   used by  spatial_join: primary
  # secondary_source: used by  spatial_join: secondary  (most common)
  # Both can point to the same layer.
  block_no:
    primary_source:
      from_layer: "Zone Grid"      # must match a layer name: value exactly
      from_field: "Zone_ID"
    secondary_source:
      from_layer: "Zone Grid"
      from_field: "Zone_ID"

  # Fields written to every feature on every layer automatically.
  # Any layer can override a specific field in its own fields: section.
  # Define whatever project-wide metadata is common to all your outputs.
  common_fields:
    Project_Name:   "Your Project"
    Organisation:   "Your Organisation"
    Prepared_By:    "Your Name or Team"
    Status:         "Draft"
```

---

## 8. The Zone / Spatial Reference System

The spatial reference system is the core mechanism that lets the executor assign zone identifiers to features across many different layers and DWG files without you having to hard-code zone values.

**What it is:** A polygon layer in your DWG (or multiple DWGs) that divides the project area into named zones. Each polygon has a zone identifier (e.g. `Z01`, `SECTOR_A`, `BLOCK_03`). The executor loads this layer first and uses it as a lookup grid throughout the rest of the run.

**What it does:**

- Any feature on any other layer can get a `Zone_ID` (or whatever you call it) assigned automatically by finding which zone polygon contains that feature's centroid. This is `spatial_join: secondary`.
- `Connection_ID` (the auto-generated unique key on every feature) incorporates the zone identifier, making it globally unique across the project.
- `from_dwg_name` field lookups are validated and corrected by spatially checking which zone a feature falls in, rather than trusting the DWG filename alone.

**When you need it:** Any layer that uses `spatial_join: secondary`, `spatial_join: primary`, or `from_dwg_name` depends on the spatial reference being loaded. If your project has no zone grid (everything is in one undivided area), you can use a single zone polygon covering the whole site and assign a constant zone ID.

**If you have no zone grid at all:** Replace `spatial_join: secondary` with a constant string field and remove `from_dwg_name` from your field configs. The spatial reference layer becomes optional.

---

## 9. Zone ID Auto-Detection from DWG Filenames

When you use `from_dwg_name: true` in a field, the executor needs to know which zone a DWG file belongs to. It extracts a Zone ID automatically from the DWG filename using a priority-ordered set of patterns.

**Zone ID format:** one uppercase letter + one or two digits + optional lowercase suffix. Examples: `A1`, `Z12`, `S3a`, `P09b`.

**Detection patterns (highest priority first):**

| Pattern in filename | Example filename | Detected ID |
|---|---|---|
| `Plot Z01` or `plot z01` | `Drawing (Plot Z01).dwg` | `Z01` |
| `Routing A2` | `Routes A2 North.dwg` | `A2` |
| `(S3a)` or `(S3a-` | `Boundary S3a.dwg` | `S3a` |
| `Block Z01` or `BlockZ01` | `Block Z01 Layout.dwg` | `Z01` |
| `_Z01_` or `-Z01-` | `Site_Z01_Layout.dwg` | `Z01` |
| `Z01.dwg` | `Z01.dwg` | `Z01` |
| Standalone token `Z01` | `Survey Z01 Final.dwg` | `Z01` |

If detection fails, a warning is printed and `from_dwg_name` returns `A?`. To recover: either rename the DWG file to include a recognisable pattern, or replace `from_dwg_name: true` with a constant string value in the field config.

**Recommended naming convention:** include the zone ID in parentheses after a keyword — e.g. `Layout (Plot Z01).dwg`, `Survey (Zone A2).dwg`. This is the most reliably detected pattern.

**Multi-zone DWGs:** A single DWG that covers multiple zones (e.g. a master routing file covering zones Z01 through Z05) is handled automatically. When `from_dwg_name` detects a zone ID from the filename but that ID has no sub-zone suffix, it spatially checks which zone polygon each feature falls in and uses that result instead of the filename-derived value. This is logged once per layer.

---

## 10. Layer Types

There are four layer types. They are distinguished by the presence (or absence) of `role:` and `derive_from:` keys.

### Type 1 — Spatial Reference Layer

The zone grid layer. One per project. Must appear before any layer that uses `spatial_join: secondary` or `spatial_join: primary`. Declare with `role: spatial_reference`.

```yaml
- name:    "Zone Grid"
  role:    spatial_reference
  locked:  false
  output:  "zone_grid.geojson"

  match_mode:   exact
  source_dwg:   drawing_a
  source_layer: "ZONE-POLYGONS"       # exact AutoCAD layer name

  fields:
    Zone_ID:
      derive:
        transform: auto_sequence
        prefix:    "Z"
        pad:       2
    Code:        "ZONE_GRID"
    Description: " "
    Area_Ha:
      calculated: area_ha
```

**Workflow tip:** once extracted and verified, lock this layer with `--lock "Zone Grid"`. On all subsequent runs the executor loads the GeoJSON from disk instead of re-reading AutoCAD, which is faster and guarantees consistency. Only unlock and re-extract if the zone boundaries change in the DWG.

**The Zone_ID field** can be any string format you choose. It will be stamped onto features of other layers via `spatial_join: secondary`. It is also incorporated into `Connection_ID` automatically. There is no requirement to use the `auto_sequence` derive — you can read Zone IDs from a block attribute, a layer name, or any other source.

---

### Type 2 — Derived Zone Boundary

Merges groups of spatial reference polygons into one dissolved polygon per declared zone cluster. This does not read from AutoCAD — it is computed from the already-loaded spatial reference features using shapely. Requires `shapely`.

Use this when your spatial reference grid has many small sub-cells (e.g. individual survey blocks) and you also need merged boundaries for larger administrative zones.

```yaml
- name:    "Sector Boundaries"
  derive_from: spatial_reference
  locked:  false
  output:  "sector_boundaries.geojson"

  # Each merge_sources entry dissolves all Zone Grid polygons
  # whose Zone_ID starts with the declared sub_plot prefix.
  merge_sources:
    - sub_plot: "Z0"      # matches Z01, Z02, Z03, ... → one merged polygon
    - sub_plot: "Z1"      # matches Z10, Z11, Z12, ...

  fields:
    Code:        "SECTOR_BND"
    Description: " "
```

---

### Type 3 — Standard Layer (point / line / polygon)

The most common type. Reads entities from one or more AutoCAD layers and writes one GeoJSON. Supports points, lines, and polygons.

**Declare the geometry type** at the layer level with `geometry:`:

| Value | Entity types read | GeoJSON output |
|---|---|---|
| `polygon` | LWPOLYLINE (closed), POLYLINE (closed) | Polygon |
| `line` | LWPOLYLINE, LINE, POLYLINE | LineString |
| `point` | INSERT (block reference), POINT | Point |

If `geometry:` is omitted, it defaults to `polygon`.

---

#### Point layer example

```yaml
- name:    "Survey Markers"
  locked:  false
  output:  "survey_markers.geojson"
  geometry: point

  match_mode:   exact
  source_dwg:   drawing_a
  source_layer: "SURVEY-PTS"
  code:         "SRV_MRK"

  geometry_params:
    only_insert: true        # only read block INSERT entities, not text/POINT

  fields:
    Code:         "SRV_MRK"
    Category:     "Survey"
    Description:  " "
    Zone_ID:
      spatial_join: secondary
    Ref_No:
      from_dwg_name: true
    Connection_ID_Link:
      spatial_join:
        method:     nearest
        from_layer: "Zone Grid"
        from_field: "Zone_ID"
    Marker_Type:
      from_attr: TYPE           # block attribute tag named TYPE
    Serial_No:
      from_attr: SERIAL
    Elevation_m:
      from_attr: ELEV
    Installed_By: " "
    Verified_By:  " "
    Remarks:      " "
```

---

#### Line layer example

```yaml
- name:    "Access Routes"
  locked:  false
  output:  "access_routes.geojson"
  geometry: line

  match_mode:   exact
  source_dwg:   routes
  source_layer: "ACCESS-ROAD"
  code:         "ACC_RTE"

  fields:
    Code:         "ACC_RTE"
    Category:     "Route"
    Type:         "Access"
    Ref_No:
      from_dwg_name: true
    Zone_ID:
      spatial_join: secondary
    Length_m:
      calculated: length_m
    Start_Node:
      spatial_join:
        method:     nearest_endpoint
        from_layer: "Survey Markers"
        from_field: "Connection_ID"
    End_Node:
      spatial_join:
        method:     nearest_endpoint
        from_layer: "Survey Markers"
        from_field: "Connection_ID"
    Surface:      " "
    Width_m:      " "
    Remarks:      " "
```

---

#### Polygon layer example

```yaml
- name:    "Site Parcels"
  locked:  false
  output:  "site_parcels.geojson"
  geometry: polygon

  match_mode:   exact
  source_dwg:   drawing_a
  source_layer: "SITE-PARCELS"
  code:         "SITE_PARC"

  geometry_params:
    min_area_sqm: 25.0       # ignore slivers and annotation polygons

  fields:
    Code:         "SITE_PARC"
    Category:     "Site"
    Ref_No:
      from_dwg_name: true
    Zone_ID:
      spatial_join: secondary
    Area_Ha:
      calculated: area_ha
    Perimeter_Km:
      calculated: perimeter_km
    Land_Use:     " "
    Owner:        " "
    Remarks:      " "
```

---

#### Using match_mode: prefix

When multiple AutoCAD layers follow a naming convention (e.g. `PIPE-150MM`, `PIPE-300MM`, `PIPE-600MM`), use `prefix` mode to capture all of them in one pass.

```yaml
- name:    "Pipelines"
  locked:  false
  output:  "pipelines.geojson"
  geometry: line

  match_mode:   prefix
  source_dwg:   routes
  source_layer: "PIPE"         # matches all layers starting with "PIPE"
  code:         "PIPE"
```

---

#### Using merge_sources to split sub-types

`merge_sources` lets you capture specific layers within a prefix group and stamp each group with its own code, sub-type, and sub-classification. Features from all sources are merged into the same output GeoJSON.

```yaml
- name:    "Pipelines"
  locked:  false
  output:  "pipelines.geojson"
  geometry: line

  match_mode:   prefix
  source_dwg:   routes
  source_layer: "PIPE"
  code:         "PIPE"

  merge_sources:
    - source_layer:      "PIPE-WATER"
      code:              "PIPE_W"
      sub_type:          "WATER"
      sub_classification: "Potable"
    - source_layer:      "PIPE-DRAINAGE"
      code:              "PIPE_D"
      sub_type:          "DRAINAGE"
      sub_classification: "Storm"
    - source_layer:      "PIPE-IRRIGATION"
      code:              "PIPE_I"
      sub_type:          "IRRIGATION"
      sub_classification: "Distribution"

  fields:
    Code:
      from_merge_source: code
    Sub_Type:
      from_merge_source: layer_subtype
    Sub_Classification:
      from_merge_source: sub_classification
    Ref_No:
      from_dwg_name: true
    Zone_ID:
      spatial_join: secondary
    Length_m:
      calculated: length_m
    Diameter_mm: " "
    Material:    " "
    Remarks:     " "
```

---

#### Layer fallbacks

If a CAD layer might have slightly different names across DWG files (e.g. `ROAD-MAJOR` in some files and `ROADS-MAJOR` in others), declare fallbacks:

```yaml
  source_layer: "ROAD-MAJOR"
  fallbacks:
    - "ROADS-MAJOR"
    - "ROAD MAJOR"
    - "MAJOR ROADS"
```

The executor tries each name in order and uses the first one found.

---

### Type 4 — Derived Child Layer

Creates N child point features from each parent feature, by reading the parent block's attribute tags. One child point is generated per non-blank attribute tag in the declared `id_fields` list. Child points are positioned evenly along the long axis of the parent feature's bounding box.

**When to use:** when one parent block in AutoCAD represents a multi-slot asset (e.g. a cabinet, rack, panel, or manifold) and each slot is identified by a separate attribute tag. Instead of one point per cabinet, you get one point per occupied slot.

```yaml
- name:    "Cabinet Ports"
  derive_from:       parent_layer
  parent_layer_name: "Equipment Cabinets"   # must match an earlier layer's name:
  locked:  false
  output:  "cabinet_ports.geojson"

  code: "CAB_PORT"

  # id_fields: which parent block attribute tags to expand into child points.
  # Format: [attribute_field_name_in_parent_properties, attribute_tag_name]
  # One child is created for each entry whose tag value is non-blank.
  id_fields:
    - [PORT_01, PORT1]
    - [PORT_02, PORT2]
    - [PORT_03, PORT3]
    - [PORT_04, PORT4]
    - [PORT_05, PORT5]
    - [PORT_06, PORT6]

  fields:
    Code:    "CAB_PORT"
    Port_ID:
      from_attr: Child_ID     # value of the matched attribute tag
    Zone_ID:
      spatial_join: secondary
    Cabinet_Ref:
      spatial_join:
        method:     nearest
        from_layer: "Equipment Cabinets"
        from_field: "Connection_ID"
    Port_Type:  " "
    Status:     " "
    Remarks:    " "
```

**How child points are positioned:**

The parent feature is expected to be a block with a polygon footprint (LWPOLYLINE in the block definition). The bounding box of that polygon is measured:
- If height ≥ width (portrait): child points are spaced along the Y axis, centred on X
- If width > height (landscape): child points are spaced along the X axis, centred on Y

Spacing uses the formula `i/(n+1)` along the long axis, so points are evenly distributed with margins at each end.

**Important:** the parent layer is always re-extracted live from AutoCAD for derived child layers, even if the parent GeoJSON already exists on disk. This is because per-attribute world coordinates are not stored in the GeoJSON output. The parent DWG must be open in AutoCAD.

---

## 11. Field Reference

All fields are declared under `fields:` in a layer config. The YAML key order is the exact property order in the output GeoJSON — this is intentional, so you can control GIS attribute table column order from the config.

Two fields are always present on every feature and do not need to be declared:

| Field | Value | Notes |
|---|---|---|
| `OBJECTID` | Integer, 1-based, resets per layer | Sequence within this layer only |
| `Connection_ID` | `{Plot_No}_{Code}_{seq:02d}` | Auto-derived after the `Plot_No` field (system-required name) and `Code` are resolved. See [Section 15](#15-the-connection_id-system) |

---

### Constant value

A plain string, number, or boolean. Written identically to every feature on the layer.

```yaml
Category:     "Civil"
Year:         2024
Is_Active:    true
Empty_Field:  " "    # space — used as a blank placeholder (not null)
Null_Field:   ~      # YAML null → written as " " in output
```

---

### from_dwg_name

Reads the Zone ID from the DWG filename. See [Section 9](#9-zone-id-auto-detection-from-dwg-filenames) for how the ID is extracted.

When the DWG covers multiple zones, the executor spatially checks which zone polygon contains each feature and uses that result instead of the filename-derived value.

```yaml
Plot_No:
  from_dwg_name: true
```

**The field must be named `Plot_No`.** The executor hardcodes this key when re-deriving `Connection_ID` after pass 1. If the field is named anything else, `Connection_ID` will not be updated with the correct zone and will retain its initial placeholder value. `Plot_No` is a system-required field name, not a user-chosen one.

---

### from_attr

Reads a block attribute tag value from the AutoCAD entity. Tag name is case-insensitive (normalised to uppercase before lookup).

```yaml
Serial_No:
  from_attr: SERIAL_NO

Asset_Code:
  from_attr: ASSET_CODE

Height_m:
  from_attr: HEIGHT
```

See [Section 17](#17-working-with-block-attributes) for how block attributes work in AutoCAD and what names to use.

---

### block_attr

Same as `from_attr` but supports a fallbacks list. The executor tries each tag name in order and uses the first non-null value found. Useful when attribute tag names vary between block definitions or DWG revisions.

```yaml
Asset_ID:
  block_attr: ASSET_ID
  fallbacks:
    - ASSETID
    - ASSET
    - ID
```

---

### spatial_join

Assigns a value to this field by finding the nearest matching feature in another layer's spatial cache. See [Section 13](#13-spatial-join-reference) for full details on how the spatial cache works and join ordering.

**Zone lookup (secondary)** — the standard way to assign a zone identifier from the zone grid. Uses the `block_no.secondary_source` config (a fixed system key in `global:`):

```yaml
Zone_ID:
  spatial_join: secondary
```

**Zone lookup (primary)** — uses `block_no.primary_source` (another fixed system key) instead of `secondary_source`:

```yaml
Zone_ID:
  spatial_join: primary
```

**Nearest feature from a named layer:**

```yaml
Nearest_Asset:
  spatial_join:
    method:     nearest
    from_layer: "Survey Markers"
    from_field: "Connection_ID"
```

**Nearest endpoint (line layers only)** — finds the nearest feature to the line's start and end points separately. The executor infers which field gets the start value and which gets the end value from the field name (looks for "start" or "end", case-insensitive):

```yaml
Start_Node:
  spatial_join:
    method:     nearest_endpoint
    from_layer: "Network Nodes"
    from_field: "Connection_ID"

End_Node:
  spatial_join:
    method:     nearest_endpoint
    from_layer: "Network Nodes"
    from_field: "Connection_ID"
```

**With a format transform** — builds a composite string from the join result and other already-resolved fields:

```yaml
Full_Ref:
  spatial_join:
    method:     nearest
    from_layer: "Zone Grid"
    from_field: "Zone_ID"
    transform:
      format: "{Plot_No}-{Zone_ID}"
```

Substitution keys are any fields resolved earlier in pass 1.

---

### calculated

Computes a geometry measurement from the feature's coordinates.

| Value | Output | Use on |
|---|---|---|
| `length_m` | Length in metres (float) | line |
| `area_ha` | Area in hectares (float) | polygon |
| `perimeter_km` | Perimeter in kilometres (float) | polygon |

```yaml
Length_m:
  calculated: length_m

Area_Ha:
  calculated: area_ha

Perimeter_Km:
  calculated: perimeter_km
```

---

### from_merge_source

Used only inside layers that have `merge_sources`. Stamps per-source metadata onto features so you can distinguish which sub-layer each feature came from.

| Key | Returns |
|---|---|
| `code` | The `code:` value from the matched merge_source entry |
| `layer_subtype` | Sub-type from: detected AC/DC suffix in CAD layer name, or `sub_type:` in merge_source config |
| `sub_classification` | The `sub_classification:` value from the merge_source entry |

```yaml
Code:
  from_merge_source: code

Sub_Type:
  from_merge_source: layer_subtype

Sub_Classification:
  from_merge_source: sub_classification
```

---

### from_config

Reads a value from a custom key defined anywhere in the `global:` section. Useful for project-wide constants that are referenced in multiple layers without repeating them.

```yaml
# In global:
global:
  default_status: "As-Built"
  inspector_name: "J. Smith"

# In a layer's fields:
Status:
  from_config: default_status

Inspector:
  from_config: inspector_name
```

---

### derive

Computes a value from other fields that have already been resolved in pass 1. Runs in pass 2. See [Section 14](#14-derive-transforms-reference) for all available transforms.

```yaml
Zone_ID:
  derive:
    transform: auto_sequence
    prefix:    "Z"
    pad:       2

Short_Ref:
  derive:
    transform:  extract_last_sequence
    from_field: Connection_ID
    prefix:     "REF-"
    pad:        3
```

---

## 12. Geometry Params Reference

Declared under `geometry_params:` in a layer config. All keys are optional.

### For point / INSERT layers

| Key | Type | Description |
|---|---|---|
| `only_insert` | bool | Accept only INSERT entities (block references). Skips POINT, TEXT, MTEXT on the same layer. Use this for all symbol/block point layers. |
| `block_name` | string | Accept only INSERT entities whose block definition name matches this value. Useful when multiple block types are on the same layer. |

### For polygon layers

| Key | Type | Description |
|---|---|---|
| `only_lwpolyline` | bool | Accept only LWPOLYLINE entities. Skips INSERT blocks. |
| `min_area_sqm` | float | Skip polygons whose area is below this threshold in square metres. Filters out annotation polygons, slivers, and drafting artefacts. |
| `target_area_sqm` | float | Accept only polygons whose area is within `tolerance` sqm of this value. |
| `tolerance` | float | Area tolerance in sqm for `target_area_sqm` matching (default: 5.0). |
| `vertex_count` | int | Accept only polygons with exactly this many vertices. |
| `forced_rotation_deg` | float | Override the INSERT entity's Rotation property (degrees) when transforming block-local polygon vertices to world coordinates. |
| `use_polyline_width` | bool | Expand an LWPOLYLINE into a polygon using its ConstantWidth property. |
| `half_width_m` | float | Default half-width in metres when `use_polyline_width` is true and the entity has no explicit width (default: 0.025). |
| `apply_width_to` | list of strings | Apply width expansion only when the current `sub_type` is in this list. |
| `section_mark_layer` | string | AutoCAD layer name of MTEXT markers used to split wide polygons into segments. |
| `snap_threshold_m` | float | Maximum distance in metres for a section mark to snap to a polyline vertex. |
| `rotate_90` | bool | Rotate polygon geometry 90 degrees. Corrects blocks whose definitions were drawn in landscape orientation. |
| `local_pts` | list of [x, y] pairs | Hardcoded block-local polygon vertices for blocks whose geometry cannot be extracted via normal means. |

### For line layers

| Key | Type | Description |
|---|---|---|
| `linetype` | string | Accept only LWPOLYLINE entities whose AutoCAD Linetype property matches this string (case-insensitive, e.g. `"DASHED"`, `"CONTINUOUS"`). |

### Example combining multiple params

```yaml
- name:    "Main Walls"
  geometry: polygon
  source_layer: "WALLS"
  geometry_params:
    only_lwpolyline: true
    min_area_sqm:    1.0
    vertex_count:    5         # only rectangular walls (4 vertices + closing = 5)
```

---

## 13. Spatial Join Reference

The spatial join engine maintains an in-memory cache of feature positions. Joins are resolved at feature level — each feature gets its own lookup result.

### Cache contents

Each registered feature is stored as:
- `centroid` — (x, y) tuple of the feature's centroid
- `polygon` — the polygon ring, if the source is a polygon layer (enables exact point-in-polygon tests)
- `insert_pt` — INSERT entity world position, if applicable
- `properties` — the full resolved property dict (all fields, all values)

### Registration order

1. **Spatial reference layer** — registered before pass 2 begins, from disk or live extraction
2. **Declared join sources** — any layer referenced by `from_layer:` in any field is extracted in pass 1
3. **Each output layer** — registered after it finishes extraction in pass 2, making it available to subsequent layers

This means: if layer B needs to join against layer A, layer A must appear earlier in the YAML config than layer B (or be a declared join source).

### spatial_join: secondary — zone assignment

Uses point-in-polygon (PIP) containment to find the zone polygon that contains the feature's centroid. Falls back to nearest centroid if the feature lies outside all zone polygons (handles edge effects and coordinate noise).

### spatial_join: nearest

Finds the feature in the named layer's cache with the smallest Euclidean distance from this feature's centroid. No radius cutoff — always returns the nearest regardless of distance. If the named layer is not in the cache, a warning is printed once and the field is left blank.

### spatial_join: nearest_endpoint

Used on line layers. Finds the nearest cached feature to the line's first vertex (start) and last vertex (end) independently. The field whose name contains "start" receives the start-end value, and the field containing "end" receives the end-point value.

### spatial_join: nearest_exclusive (post-processing)

A batch mode run after a layer is fully extracted. Each source feature is matched to at most one target feature — one-to-one assignment within a zone boundary. Not available as a direct field resolver; configured separately in layer config. Used when you need guaranteed uniqueness (e.g. each route segment connects to exactly one node at each end, with no two segments sharing the same node).

### Miss warnings

When a spatial join returns no result (layer not in cache, or layer in cache but the `from_field` returned empty), a warning is printed once per (field_name, from_layer) pair. Repeated misses on the same combination are suppressed to avoid flooding the output.

---

## 14. Derive Transforms Reference

`derive` transforms run in pass 2, after all pass-1 fields (constants, block attrs, spatial joins, calculations) are resolved. They compute values from other resolved fields.

---

### `auto_sequence`

Generates a zero-padded sequence string using the feature's `OBJECTID`.

```yaml
Zone_ID:
  derive:
    transform: auto_sequence
    prefix:    "Z"       # string prepended before the number
    pad:       2         # minimum digit width (zero-padded)
# OBJECTID=4 → "Z04"
# OBJECTID=12 → "Z12"
```

---

### `block_no_from_id`

Parses a `B{n}` pattern from a field value and formats it as `{ZoneID}_BLK{nn}`.

```yaml
Zone_Block:
  derive:
    transform:  block_no_from_id
    from_field: Attribute_01
# Attribute_01="B07", ZoneID="Z02" → "Z02_BLK07"
```

---

### `block_no_from_layer_name`

Extracts trailing digits from the matched AutoCAD layer name and formats as `{ZoneID}_BLK{nn}`.

```yaml
Zone_Block:
  derive:
    transform: block_no_from_layer_name
# CAD layer "SURVEY-GRID-09", ZoneID="A1" → "A1_BLK09"
```

---

### `strip_last_segment`

Removes the last two separator-delimited segments from a string. Useful for deriving parent identifiers from child connection IDs.

```yaml
Parent_Ref:
  derive:
    transform:  strip_last_segment
    from_field: Connection_ID
    separator:  "_"
# "Z01_ASSET_TYPE_05" → "Z01_ASSET"
```

---

### `count_filled`

Counts how many fields in a list have non-null values and maps the count to a declared string value.

```yaml
Occupancy_Status:
  derive:
    transform:   count_filled
    from_fields: [SLOT_01, SLOT_02, SLOT_03, SLOT_04]
    value_map:
      0: "Empty"
      1: "Partial"
      2: "Half"
      4: "Full"
```

---

### `extract_suffix`

Extracts the numeric suffix from a `_BLKnn`-formatted value and prepends a prefix.

```yaml
Short_Ref:
  derive:
    transform:  extract_suffix
    from_field: Zone_ID
    prefix:     "BL"
# "Z01_BLK05" → "BL05"
```

---

### `format_reference_id`

Formats a `_BLKnn` value into a reference code: `{prefix}{ZoneID}-BL-{nn}`.

```yaml
Doc_Ref:
  derive:
    transform:  format_reference_id
    from_field: Zone_ID
    prefix:     "DOC-"
# Zone_ID="Z01_BLK03", ZoneID="Z01" → "DOC-Z01-BL-03"
```

---

### `block_no_to_connection`

Converts `ZONE_BLKnn` to `ZONE-BLnn`.

```yaml
Linked_Node:
  derive:
    transform:  block_no_to_connection
    from_field: Zone_ID
# "Z01_BLK03" → "Z01-BL03"
```

---

### `block_no_to_prefixed_connection`

Like `block_no_to_connection` but prepends a custom prefix.

```yaml
Asset_Ref:
  derive:
    transform:  block_no_to_prefixed_connection
    from_field: Zone_ID
    prefix:     "ASSET-"
# "Z01_BLK03" → "ASSET-Z01-BLK03"
```

---

### `extract_last_sequence`

Takes the last `_`-delimited numeric segment of a string, optionally reformats it with a prefix and zero-padding.

```yaml
Seq_No:
  derive:
    transform:  extract_last_sequence
    from_field: Connection_ID
    prefix:     "SEQ-"
    pad:        3
# "Z01_ASSET_TYPE_07" → "SEQ-007"
```

---

### `prepend_plot`

Prepends the Zone ID to a block text value using a format string.

```yaml
Label:
  derive:
    transform: prepend_plot
    format:    "{plot_no}-{text}"
# ZoneID="Z02", block_text="MAIN-GATE" → "Z02-MAIN-GATE"
```

---

## 15. The Connection_ID System

Every feature automatically receives a `Connection_ID` field. This is a composite string key that uniquely identifies a feature within the project.

**Format:** `{Zone_ID}_{Code}_{seq:02d}`

- `Zone_ID` — the resolved value of the field named `Plot_No` (system-required name — see `from_dwg_name` in [Section 11](#11-field-reference))
- `Code` — the layer's `code:` value, or the per-source code from `merge_sources`
- `seq` — the `OBJECTID` for this feature, zero-padded to 2 digits

**Examples:**
- `Z01_ASSET_01` — first asset in zone Z01
- `Z03_PIPE_W_17` — 17th water pipe feature in zone Z03
- `Z02_SRV_MRK_04` — 4th survey marker in zone Z02

`Connection_ID` is seeded at the start of `resolve()` and then **re-derived after pass 1** once `Plot_No` and `Code` have been resolved to their final values. This means the final `Connection_ID` always reflects the actual resolved zone and code, not the DWG-filename value or a placeholder.

**Why it matters:** `Connection_ID` is used as the join key in `spatial_join: nearest` configurations. When a route needs to reference the asset it connects to, it joins on `Connection_ID`. This makes inter-layer linkage fully automated — as long as features are spatially close, their connection references are set correctly.

---

## 16. Multi-DWG Projects

Most real projects have multiple DWG files — typically one per zone or one per drawing type (layout, routing, boundary). The executor handles this transparently.

### Declaring multiple DWGs

```yaml
global:
  source_dwgs:
    zone_01:    "C:/Project/DWGs/Layout_Zone01.dwg"
    zone_02:    "C:/Project/DWGs/Layout_Zone02.dwg"
    zone_03:    "C:/Project/DWGs/Layout_Zone03.dwg"
    routing:    "C:/Project/DWGs/Routes_All.dwg"
    boundary:   "C:/Project/DWGs/Boundary.dwg"
```

### Using multiple DWGs for a single layer

A standard layer can only reference one `source_dwg`. To extract the same layer type from multiple DWGs into one output file, use `merge_sources`:

```yaml
- name:    "All Assets"
  locked:  false
  output:  "all_assets.geojson"
  geometry: point

  match_mode:   exact
  source_layer: "ASSETS"
  code:         "ASSET"

  merge_sources:
    - source_dwg: zone_01
      source_layer: "ASSETS"
      code:         "ASSET"
    - source_dwg: zone_02
      source_layer: "ASSETS"
      code:         "ASSET"
    - source_dwg: zone_03
      source_layer: "ASSETS"
      code:         "ASSET"

  fields:
    Code:     "ASSET"
    Plot_No:
      from_dwg_name: true
    Zone_ID:
      spatial_join: secondary
    ...
```

This combines assets from all three zone DWGs into a single `all_assets.geojson`.

### Zone ID per DWG

The `from_dwg_name: true` field resolver returns a different Zone ID for each DWG based on the filename. Combined with `spatial_join: secondary` for the zone polygon lookup, this means features from different DWGs automatically receive the correct zone assignment.

### DWG not open warning

If a DWG in `source_dwgs` is not open in AutoCAD when the executor runs, that specific DWG is skipped. Features from other DWGs in the same merge_sources list continue to be extracted. A warning is printed for each skipped DWG.

---

## 17. Working with Block Attributes

AutoCAD blocks with attributes are the primary source of per-feature metadata. Understanding how attributes work is essential for configuring `from_attr` and `block_attr` fields.

### What block attributes are

A **block** in AutoCAD is a named symbol definition. When an instance of that block is inserted into a drawing, that instance is an `INSERT` entity. A block definition can contain **attribute definitions** (ATTDEF) — these are named fields that each inserted instance fills with its own value.

For example, a "Survey Marker" block might have attribute definitions:
- `SERIAL_NO` — the marker's serial number
- `TYPE` — marker type (permanent, temporary, etc.)
- `ELEVATION` — ground elevation at the marker

When 50 survey markers are placed in the drawing, each INSERT entity holds its own values for `SERIAL_NO`, `TYPE`, and `ELEVATION`.

### Targeting a block layer and extracting user-specified data

This is the core workflow for any DWG layer populated with symbol blocks (survey markers, equipment symbols, structure references, etc.):

**Step 1 — Identify the layer and block type.**  
Run `python executor.py config.yaml --dwg-layers` to list layers in the open DWG. Find the layer that contains your block INSERT entities. In AutoCAD, double-click one entity on that layer to open the attribute editor and note the tag names.

**Step 2 — Configure the layer in YAML.**  
Use `geometry: point`, set `only_insert: true` (so only block references are read, not any stray text or POINT entities on the same layer), and optionally filter by block definition name using `block_name` if the layer contains mixed block types.

**Step 3 — List the attribute tags you want, named however you choose.**  
In the `fields:` section, each entry maps a tag from the block to whatever output field name you specify. The tag name (right side, `from_attr:`) comes from AutoCAD's block definition. The field name (left side) is entirely yours — it will become the property key in the GeoJSON output.

```yaml
- name: "Survey Markers"
  source_dwg: "C:/path/to/your/drawing.dwg"
  layer_name: "SURVEY_MARKERS"
  geometry: point
  output: "output/survey_markers.geojson"
  geometry_params:
    only_insert: true          # accept only block INSERT entities
    block_name: "MRK_STD"     # optional: only this block definition name
  fields:
    - Serial_Number: { from_attr: SERIAL_NO }
    - Marker_Type:   { from_attr: TYPE }
    - Elevation_m:   { from_attr: ELEVATION }
    - Installed_By:  { from_attr: SURVEYOR }
    - Notes:         { from_attr: REMARKS }
    - Status:        { constant: "ACTIVE" }
```

The executor opens each INSERT entity on `SURVEY_MARKERS`, reads the listed attribute tags directly from the live AutoCAD document, and writes them to GeoJSON properties — in the order you specified, with the names you chose. Any tag omitted from `fields:` is silently ignored; only the tags you explicitly list appear in the output.

**Result** — one GeoJSON feature per block instance, properties shaped exactly as you declared:

```json
{
  "type": "Feature",
  "geometry": { "type": "Point", "coordinates": [453201.4, 2841093.7] },
  "properties": {
    "OBJECTID": 1,
    "Connection_ID": "Z01_MRK_01",
    "Serial_Number": "MRK-2041",
    "Marker_Type": "Permanent",
    "Elevation_m": "412.5",
    "Installed_By": "SURVEY_TEAM_A",
    "Notes": "Reference monument",
    "Status": "ACTIVE"
  }
}
```

> **Tip:** To discover all attribute tags on an unknown block without checking AutoCAD manually, add a temporary diagnostic field:
> ```yaml
> - Debug_All: { block_attr: [UNKNOWN_TAG_1, UNKNOWN_TAG_2] }
> ```
> Or use AutoCAD's `LIST` command (type `LIST`, click the entity, press Enter) to see every tag name and its current value.

### Finding attribute tag names

Use `--dwg-layers` to see what layers are in your DWG, then examine a block entity in AutoCAD to see its attribute tags:

1. In AutoCAD, double-click an INSERT entity → the attribute editor shows tag names and values
2. Or use `LIST` command in AutoCAD on the entity to see tag names

Alternatively, add a temporary `from_attr` field in your config with a tag name you expect — if it returns blank, the tag name is wrong.

### Tag name case

Tag names in `from_attr` and `block_attr` are normalised to uppercase before lookup. So `from_attr: serial_no`, `from_attr: Serial_No`, and `from_attr: SERIAL_NO` all do the same thing.

### Blocks without attributes

If a block has no attribute definitions, `from_attr` and `block_attr` will return blank. In this case, all per-feature metadata must come from other sources (constants, spatial joins, or the block's layer/name).

### Nested block attributes

The executor reads attributes from the INSERT entity itself. Attributes nested inside a block definition (ATTDEF) are read via the AutoCAD COM `Attributes` collection. Nested block references inside a block definition are not recursed.

---

## 18. CLI Reference

```
python executor.py [config.yaml] [options]
```

The config path is optional. If omitted, the executor searches the script folder for `config.yaml` or a single `*.yaml` file.

### Commands

| Command | Description |
|---|---|
| *(no options)* | Launch the interactive layer selector |
| `--list` | Print all layers with lock status, source, and exit |
| `--status` | Same as `--list` |
| `--run` | Run all unlocked layers without interaction |
| `--run all` | Same as `--run` |
| `--layers "A" "B"` | Run the named layers regardless of lock state |
| `--unlock "Name"` | Set `locked: false` for this layer in the YAML, then exit |
| `--lock "Name"` | Set `locked: true` for this layer in the YAML, then exit |
| `--unlock-all` | Set `locked: false` for all layers in the YAML, then exit |
| `--lock-all` | Set `locked: true` for all layers in the YAML, then exit |
| `--dwg-layers key` | Print all CAD layer names and entity type counts from the DWG aliased as `key`, then exit |

### Layer name matching

Layer names passed to `--layers`, `--unlock`, and `--lock` are fuzzy-matched against the config. You do not need to type the full exact name — a close match is accepted. If the match is ambiguous, the closest name is used; if nothing is close, an error is reported.

---

### Interactive mode

Running with no `--run` or `--layers` argument launches the interactive layer selector. AutoCAD is connected first, then a numbered menu appears:

```
  [1]  [ON ] 🔓 Zone Grid
  [2]  [OFF] 🔒 Survey Markers
  [3]  [ON ] 🔓 Access Routes
  [4]  [OFF] 🔒 Site Parcels
──────────────────────────────────────────────────────────────
  [A] All ON   [N] All OFF   [X] Run
============================================================
Choice:
```

- Type a number to toggle that layer on/off
- `A` — select all unlocked layers
- `N` — deselect all
- `X` — confirm selection and run

Locked layers (🔒) cannot be selected in the interactive mode. Unlock them first with `--unlock`.

---

### Practical command sequences

**Full project from scratch:**
```
python executor.py config.yaml --dwg-layers drawing_a    # inspect layer names
python executor.py config.yaml --unlock "Zone Grid"
python executor.py config.yaml --layers "Zone Grid"      # extract zone grid first
python executor.py config.yaml --lock "Zone Grid"
python executor.py config.yaml --unlock "Survey Markers"
python executor.py config.yaml --unlock "Access Routes"
python executor.py config.yaml --run                     # run all unlocked
python executor.py config.yaml --lock-all
```

**Re-run a single layer:**
```
python executor.py config.yaml --unlock "Site Parcels"
python executor.py config.yaml --layers "Site Parcels"
python executor.py config.yaml --lock "Site Parcels"
```

**Check status before a run:**
```
python executor.py config.yaml --status
```

**Unlock everything and run all at once (use with care):**
```
python executor.py config.yaml --unlock-all
python executor.py config.yaml --run
python executor.py config.yaml --lock-all
```

---

## 19. Lock System

### What locks do

Every layer in the YAML has a `locked:` boolean. When `locked: true`, the executor will not write that layer's output file even if the layer is selected. This prevents accidental overwrites of verified outputs when re-running for other layers.

### Default state

New layers should be written with `locked: true`. The executor will warn you if you try to run them. Unlock explicitly when you're ready to extract.

### How locks are stored

The lock state is stored directly in the YAML config file. `--unlock` and `--lock` rewrite the `locked:` line for that layer in the file. No separate lock file exists. You can also edit `locked:` values manually in a text editor — any `true`/`false` value works.

### Exception: derive_from layers

Layers with `derive_from:` (`spatial_reference` or `parent_layer`) ignore the lock flag — they are always allowed to run and overwrite because they perform computation, not DWG extraction. There is no risk of overwriting raw extraction data.

### Recommended workflow for a multi-layer project

1. Extract and verify the spatial reference layer (zone grid) first; lock it
2. Extract the first batch of layers; verify output in QGIS or another GIS tool
3. Lock verified layers; move to the next batch
4. Repeat until all layers are extracted and locked
5. For a full re-extraction (e.g. after DWG updates), use `--unlock-all` then `--run`

---

## 20. Output Format

Each layer writes one GeoJSON file. The file follows RFC 7946 with an additional `crs` member for compatibility with GIS tools that require explicit CRS declarations.

### File structure

```json
{
  "type": "FeatureCollection",
  "name": "survey_markers",
  "crs": {
    "type": "name",
    "properties": {
      "name": "urn:ogc:def:crs:EPSG::32632"
    }
  },
  "features": [
    {
      "type": "Feature",
      "geometry": {
        "type": "Point",
        "coordinates": [412345.67, 2345678.90]
      },
      "properties": {
        "OBJECTID": 1,
        "Connection_ID": "Z01_SRV_MRK_01",
        "Code": "SRV_MRK",
        "Category": "Survey",
        "Zone_ID": "Z01",
        "Plot_No": "Z01",
        "Serial_No": "MRK-001",
        "Type": "Permanent",
        "Elevation_m": "412.50"
      }
    }
  ]
}
```

### Conventions

| Item | Behaviour |
|---|---|
| Coordinates | In the CRS declared in `global.crs` |
| Null values | Written as `" "` (a single space string) — not JSON null |
| OBJECTID | Resets to 1 for each layer independently |
| Connection_ID | Unique within a layer; globally unique within a project when zone IDs are unique |
| File name | Set by `output:` in the layer config; defaults to `LayerName.geojson` with spaces replaced by underscores |
| CRS EPSG code | Extracted from `global.crs` (takes everything after the last `:`) |
| Feature ordering | Matches the AutoCAD entity iteration order within each DWG layer |
| Overwrite | Existing file is overwritten silently on each run (for unlocked layers) |

### Geometry types

| Layer geometry | GeoJSON type | Coordinates |
|---|---|---|
| `polygon` | `Polygon` | Ring of `[x, y]` pairs, first = last (closed) |
| `line` | `LineString` | Ordered `[x, y]` pairs from polyline vertices |
| `point` | `Point` | Single `[x, y]` from INSERT insertion point |

---

## 21. Config Management and Version Control

### Never commit real project configs

Real project configs contain client-specific data — DWG file paths, site names, field values, zone identifiers. The `.gitignore` excludes all `tora_config_*.yaml` files except the sample. If you name your config `config.yaml` or any other name not matching that pattern, add it to `.gitignore` manually.

### What to commit

- `executor.py` — the tool itself
- `tora_config_sample.yaml` — the generic template (already tracked)
- `README.md` — this documentation
- `.gitignore`

### What not to commit

- Any config file with real DWG paths, client names, or project data
- Output GeoJSON files (already in `.gitignore`)
- `__pycache__/`, `.pyc` files (already in `.gitignore`)

### Config versioning

Because the YAML config file controls the entire schema, it is effectively the project's data specification. Keep it in a separate version-controlled location (internal company repository, SharePoint, etc.) alongside the output GeoJSON and delivery documentation.

### Working in a team

If multiple people need to run the executor on the same project:
- Share the config file through internal channels (not a public repo)
- Lock layers that one team member has already verified to prevent another from accidentally overwriting them
- Use `--status` to communicate which layers are complete

---

## 22. Performance Notes

### Typical throughput

Performance depends on entity count, DWG complexity, and AutoCAD COM overhead. As a rough guide:
- Simple point layers (INSERT entities, no complex geometry): 200–500 features/second
- Polygon layers (LWPOLYLINE with many vertices): 50–200 features/second
- Layers requiring spatial joins: slightly slower per feature due to nearest-neighbour lookups

### Spatial reference layer caching

The biggest single performance gain is locking the spatial reference layer once it is extracted. Subsequent runs load it from disk in milliseconds rather than re-reading AutoCAD.

### Progress display

The executor shows two live progress bars: one for the current layer's entity count, one for the overall layer count. Both show elapsed time and estimated time remaining. No additional setup is needed.

### Large DWGs

AutoCAD COM becomes slow when a DWG contains tens of thousands of entities across many layers. To improve performance on large DWGs:
- Use `only_insert: true` on point layers to skip text and other entity types
- Use `min_area_sqm` on polygon layers to skip small annotation polygons early
- Use `linetype` filtering on line layers when only a specific linetype is needed

### Multiple DWGs in one session

All DWGs can be open simultaneously. The executor switches between them programmatically. There is no need to close and reopen files between layers.

---

## 23. Troubleshooting

---

### `Cannot connect to AutoCAD`

AutoCAD is not running or is not accessible via COM.

**Fix:** start AutoCAD, open your DWG files, then run the executor again. If AutoCAD is running but the error persists:
- Ensure Python and AutoCAD are running as the same Windows user (not one elevated, one not)
- Try running the executor from a standard Command Prompt rather than a terminal inside an IDE
- Restart AutoCAD and try again

---

### `DWG not open in AutoCAD: Drawing_A.dwg`

AutoCAD is running but this specific document is not open.

**Fix:** open the file in AutoCAD and run again. Path matching is case-insensitive and falls back to filename-only matching if the full path does not match. The file must be open — the executor cannot open DWG files itself.

---

### `Layer 'MY-LAYER' not found in 'drawing_a'`

The `source_layer` value in your config does not match any layer name in that DWG.

**Fix:** run `python executor.py config.yaml --dwg-layers drawing_a` to see the exact list of available layer names. AutoCAD layer names are case-sensitive and include all punctuation, spaces, and hyphens. Copy the name exactly.

---

### `Could not detect plot from Drawing_A.dwg`

The Zone ID could not be extracted from the DWG filename.

**Fix:** either rename the DWG file to include a recognisable zone ID pattern (e.g. `Drawing (Plot Z01).dwg`), or replace `from_dwg_name: true` with a constant string value in the relevant field config:
```yaml
Plot_No: "Z01"
```

---

### `spatial_join MISS: 'Zone_ID' — layer 'Zone Grid' not found in cache`

A spatial join tried to look up a zone but the zone grid is not loaded.

**Fix:** the spatial reference layer must be extracted before any layer that uses `spatial_join: secondary`. Run the zone grid layer first. If its GeoJSON already exists on disk, the executor loads it automatically — verify the file exists at the path `output_dir/zone_grid.geojson` (or whatever `output:` value you configured). Also confirm that `global.block_no.secondary_source.from_layer` matches the zone grid layer's `name:` value exactly.

---

### `spatial_join MISS: 'Zone_ID' — layer 'Zone Grid' found in cache but field 'Zone_ID' returned empty`

The zone grid layer is cached but the field name in the cache does not match what was requested.

**Fix:** check that `global.block_no.secondary_source.from_field` matches the exact field name in the zone grid layer's `fields:` section. Field names are case-sensitive.

---

### Features extracted but all points at the same location (0, 0 or centroid)

**If using INSERT blocks:** add `geometry_params: only_insert: true`. Without this, MTEXT, attribute text, and other entity types on the same layer are also read — their "coordinates" are text insertion points, not block positions, and they may collapse to a default location.

**If using the derived child layer:** the parent DWG must be open in AutoCAD. The derived child layer always re-extracts the parent live — it cannot use the cached GeoJSON. If the parent DWG is not open, extraction fails silently and child point positions default to (0, 0).

---

### Polygon geometry looks wrong / distorted

**Rotation issue:** if polygons appear rotated or mirrored, the block definition may use a non-standard orientation. Try:
```yaml
geometry_params:
  rotate_90: true
```
or set `forced_rotation_deg` to the correct rotation value.

**Scale issue:** the executor uses the INSERT entity's Scale factors when transforming block-local vertices. If your block definition uses non-uniform scaling, geometry may be distorted. Verify the block scale in AutoCAD (Properties → Scale X, Y, Z).

---

### 0 features extracted for a layer

The executor ran without error but wrote an empty GeoJSON.

Check the console output for counts:
- `skipped` count — `geometry_params` filters eliminated all entities
- No entities found at all on that layer — the CAD layer exists but is empty

**Diagnosis steps:**
1. Remove all `geometry_params` temporarily and run again to confirm entities exist
2. Check `only_insert: true` — if the layer has no INSERT entities, this filters everything
3. Check `min_area_sqm` — if set too high, all polygons are filtered
4. Add filters back one at a time to find which one is eliminating features

---

### `shapely` import error

A `derive_from: spatial_reference` layer requires shapely.

**Fix:** `pip install shapely`. Shapely is only imported when a zone boundary dissolve layer is run — all other layer types work without it.

---

### `Multiple YAML files found` error on startup

The config auto-discovery found more than one `*.yaml` file in the script folder.

**Fix:** specify the config path explicitly: `python executor.py my_project_config.yaml --run`

---

### Field values in output are `" "` (space) instead of actual data

The block attribute tag was not found on this entity.

**Causes:**
- The tag name in `from_attr` is wrong — verify with AutoCAD's attribute editor
- This entity is not an INSERT / has no attributes (e.g. it is a POINT or MTEXT entity)
- The attribute exists but was left blank in the DWG

**Diagnosis:** temporarily add a catch-all field `Debug_Attr: { from_attr: YOUR_TAG }` and verify the output. Use AutoCAD's `LIST` command on a representative entity to see what tags it has.

---

### `Connection_ID` has `A?` in it

The Zone ID was not resolved — the fallback `A?` value is in use.

**Causes:**
- `from_dwg_name` could not extract a Zone ID from the DWG filename (see the `Could not detect plot` troubleshooting entry above)
- The spatial reference layer is not loaded, so spatial fallback failed

**Fix:** either rename the DWG, replace `from_dwg_name: true` with a constant, or ensure the zone grid is loaded before running this layer.

---

## 24. Project Setup Checklist

Use this checklist when starting a new project.

### Before writing the config

- [ ] AutoCAD is installed and your DWG files are accessible
- [ ] You know which DWG layer contains the zone/block grid (if your project has one)
- [ ] You have a list of the output layers you need to produce
- [ ] You know the coordinate reference system (UTM zone) for your project area

### Config setup

- [ ] All DWG file paths in `source_dwgs` are correct absolute paths
- [ ] `crs` is set to the correct EPSG code for your project area
- [ ] `output_dir` is set to a writable folder
- [ ] `global.block_no.secondary_source.from_layer` matches the spatial reference layer's `name:` exactly
- [ ] Layer names in `source_layer` have been verified with `--dwg-layers` (not guessed)
- [ ] All layers start with `locked: true`
- [ ] The spatial reference layer appears before any layer using `spatial_join: secondary` in the YAML list
- [ ] Any layer used as a join source (`from_layer:`) appears before the layers that join against it

### Before running

- [ ] AutoCAD is open
- [ ] All DWGs listed in `source_dwgs` are open in AutoCAD
- [ ] Only the intended layers are unlocked (`--status` to check)
- [ ] The config file is not in a publicly visible git repository

### After running

- [ ] Output GeoJSON files are verified in QGIS, ArcGIS, or another GIS tool
- [ ] Feature counts look correct (no suspiciously low or zero counts)
- [ ] Zone ID and Connection_ID values are correct on a sample of features
- [ ] Verified layers are locked with `--lock "Layer Name"`
- [ ] Config file is backed up or stored in the project's internal document management system
