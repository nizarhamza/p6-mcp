"""Public demo project DEMO-SS225 (225/63 kV GIS substation, 214 activities): two XER files made with
the server's CPM engine, and a test database with both imported.

  DEMO-SS225_BL.xer  the baseline: nothing started, data date = project start (2 Feb 2026)
  DEMO-SS225.xer     the update at the data date 26 Oct 2026: actuals, remaining work, late items,
                     driving path and float paths

Every name and value is invented, resources included (labor crews, one crane, one cable material).
Dates, late dates and float come from the CPM engine (p6mcp.server._Cpm, P6's F9 rules) run in
memory: first on the baseline network, then on an "as performed" network (longer durations and late
starts) that gives the actual dates before the data date, then on the update. Everything is seeded,
so a run always gives the same files.

build_db(path) = make_db.build(path), the P6 columns the demo carries that make_db leaves out, then
p6_import_xer: the update as a new project in EPS-DEMO, the baseline as its project baseline. The
server must not be configured for another database: build_db points it at path (a throwaway config
when the process has none, never the user's settings).

    python tests/fixtures/make_demo.py [output folder]   # the two XER files and demo.db
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import make_db  # noqa: E402

PROJ, PROJ_BL = "DEMO-SS225", "DEMO-SS225_BL"
NAME = "225/63 kV GIS Substation - Demo Project"
EPS = "EPS-DEMO"  # EPS node of make_db
START = datetime(2026, 2, 2, 8, 0)  # notice to proceed, Monday
DD = datetime(2026, 10, 26, 8, 0)  # data date of the update
LAST_F9 = datetime(2026, 10, 26, 17, 30)
CAL_ID, CAL_NAME = 1, "DEMO 5-Day Workweek 8h"
HOLIDAYS = ["2026-04-06", "2026-05-01", "2026-05-14", "2026-05-25", "2026-07-14", "2026-11-11", "2026-12-24",
            "2026-12-25", "2026-12-28", "2026-12-29", "2026-12-30", "2026-12-31", "2027-01-01", "2027-03-29",
            "2027-05-06", "2027-05-17", "2027-07-14", "2027-11-11"]
HPD = 8.0
# invented resources: short name -> (name, type, cost per unit)
RES = {"DM-DESIGN": ("Demo design engineers", "RT_Labor", 80),
       "DM-REVIEW": ("Demo engineering reviewers", "RT_Labor", 75),
       "DM-PM": ("Demo project managers", "RT_Labor", 110), "DM-BUYER": ("Demo buyers", "RT_Labor", 60),
       "DM-QAQC": ("Demo factory inspectors", "RT_Labor", 85), "DM-LABOR": ("Demo site labor crew", "RT_Labor", 28),
       "DM-CIVIL": ("Demo concrete crew", "RT_Labor", 40), "DM-BUILD": ("Demo building trades crew", "RT_Labor", 38),
       "DM-ELEC": ("Demo electricians", "RT_Labor", 45), "DM-SUPV": ("Demo site supervisors", "RT_Labor", 70),
       "DM-SUPV2": ("Demo assistant supervisors", "RT_Labor", 50),
       "DM-COMLEAD": ("Demo lead commissioning engineers", "RT_Labor", 95),
       "DM-COMTEC": ("Demo commissioning technicians", "RT_Labor", 90),
       "DM-CRANE": ("Demo 80 t mobile crane", "RT_Equip", 150), "DM-CABLE": ("Demo cables (m)", "RT_Mat", 6)}
MATERIAL_QTY = {"CON-CBL-1000": 18000.0, "CON-HV-1060": 2400.0}  # material quantities (not per hour)
OPTS = {"sched_outer_depend_type": "SD_Both", "sched_open_critical_flag": "N", "sched_lag_early_start_flag": "Y",
        "sched_retained_logic": "Y", "sched_setplantoforecast": "N", "sched_float_type": "FT_FF",
        "sched_calendar_on_relationship_lag": "rcal_Predecessor", "sched_use_expect_end_flag": "Y",
        "sched_progress_override": "N", "sched_use_project_end_date_for_float": "Y"}
FLOAT_PATHS = 5  # multiple float paths stored on the update (total float mode)

# ---------------------------------------------------------------- network
WBS = []  # (key, parent key, short name, name)
ACTS = {}  # code -> {code, name, wbs, days, type, res [(rsrc, people)]}
LINKS = []  # (pred, succ, type, lag days)


def wbs(key, parent, short, name):
    WBS.append((key, parent, short, name))


def act(code, name, w, days, res=(), typ="TT_Task"):
    assert code not in ACTS, code
    ACTS[code] = {"code": code, "name": name, "wbs": w, "days": days, "type": typ, "res": list(res)}
    return code


def link(p, q, typ="PR_FS", lag=0):
    LINKS.append((p, q, typ, lag))


wbs("PRJ", None, PROJ, NAME)
for key, short, name in (("PM", "PMT", "Project Management"), ("MS", "MS", "Contract Milestones"),
                         ("ENG", "ENG", "Engineering and Design"), ("PRC", "PRC", "Procurement and Logistics"),
                         ("CON", "CON", "Construction and Installation"),
                         ("COM", "COM", "Testing, Commissioning and Handover")):
    wbs(key, "PRJ", short, name)
for key, par, short, name in (
        ("ENG.CVL", "ENG", "CVL", "Civil and Structural Design"),
        ("ENG.PRI", "ENG", "PRI", "Primary Electrical Design"),
        ("ENG.SEC", "ENG", "SEC", "Protection, Control and SCADA Design"),
        ("PRC.HV", "PRC", "HV", "HV Equipment and Structures"), ("PRC.MV", "PRC", "MV", "MV, LV and Bulk Materials"),
        ("PRC.SEC", "PRC", "SEC", "Secondary Systems"),
        ("CON.CIV", "CON", "CVL", "Civil and Building Works"),
        ("CON.EM", "CON", "EM", "Electromechanical Installation"),
        ("CON.CBL", "CON", "CBL", "Cabling"),
        ("CON.CIV.SITE", "CON.CIV", "SITE", "Site Preparation"), ("CON.CIV.FDN", "CON.CIV", "FDN", "Foundations"),
        ("CON.CIV.BLD", "CON.CIV", "BLD", "Buildings and Roads"),
        ("CON.EM.HV", "CON.EM", "HV", "HV Equipment Installation"),
        ("CON.EM.SEC", "CON.EM", "SEC", "MV, LV and Secondary Installation"),
        ("COM.PRE", "COM", "PRE", "Pre-commissioning Tests"), ("COM.ENR", "COM", "ENR", "Energization"),
        ("COM.HO", "COM", "HO", "Handover and Closeout")):
    wbs(key, par, short, name)

# milestones
MS = {k: act(f"MS-{1000 + 10 * i}", n, "MS", 0, typ="TT_Mile" if i == 0 else "TT_FinMile") for i, (k, n) in enumerate((
    ("ntp", "Notice to proceed"), ("civ_ifc", "Civil design issued for construction"),
    ("ele_ifc", "Electrical design issued for construction"), ("gis_del", "225 kV GIS delivered to site"),
    ("tr_del", "Power transformers delivered to site"), ("civ_done", "Civil works complete"),
    ("mc", "Mechanical completion"), ("rfe", "Ready for energization"), ("energ", "First energization 225 kV"),
    ("pa", "Provisional acceptance"), ("end", "Project completion")))}

# project management
pm = {k: act(f"PM-{1000 + 10 * i}", n, "PM", d, [("DM-PM", 1)]) for i, (k, n, d) in enumerate((
    ("pep", "Project execution plan", 15), ("hse", "HSE plan", 10), ("qp", "Quality plan", 10),
    ("proc", "Procurement plan approval", 10)))}
for k in ("pep", "hse", "qp"):
    link(MS["ntp"], pm[k])
link(pm["pep"], pm["proc"])

# engineering: each document = preparation, client review, revise and issue for construction
DOCS = {
    "CVL": [("svy", "Topographic survey and geotechnical report", 15), ("earth", "Earthing grid design", 15),
            ("gisb", "GIS building structural design", 25), ("ctrlb", "Control building design", 20),
            ("trf", "Transformer foundations and oil pit design", 15),
            ("trench", "Cable trench, drainage and roads design", 20)],
    "PRI": [("sld", "Single line diagram", 10), ("lay", "Substation layout and sections", 20),
            ("gis225", "225 kV GIS technical specification", 15), ("gis63", "63 kV GIS technical specification", 15),
            ("tr", "Power transformer technical specification", 15), ("hvc", "HV cable system specification", 10),
            ("mvs", "MV switchgear and auxiliary transformer specification", 10),
            ("ins", "Insulation coordination and lightning protection study", 15), ("est", "Earthing study", 10)],
    "SEC": [("pcph", "Protection and control philosophy", 15),
            ("pcp", "Protection and control panel specification", 15),
            ("scada", "SCADA and telecom specification", 15), ("schem", "Schematic and wiring diagrams", 30),
            ("relay", "Protection relay settings", 15), ("aux", "LV AC/DC auxiliary systems design", 15)]}
D = {}  # doc key -> (prep, review, ifc)
for disc, docs in DOCS.items():
    n = 1000
    for key, title, prep in docs:
        rev = 15 if key == "schem" else 10
        ifc = 10 if key in ("schem", "gisb") else 5
        codes = []
        for step, days, res in (("preparation", prep, [("DM-DESIGN", 2)]), ("client review", rev, [("DM-REVIEW", 0.5)]),
                                ("revise and issue for construction", ifc, [("DM-REVIEW", 1)])):
            codes.append(act(f"ENG-{disc}-{n}", f"{title} - {step}", f"ENG.{disc}", days, res))
            n += 10
        link(codes[0], codes[1])
        link(codes[1], codes[2])
        D[key] = codes
for k in ("sld", "svy", "pcph"):
    link(MS["ntp"], D[k][0])
for k in ("lay", "gis225", "gis63", "tr", "hvc", "mvs"):
    link(D["sld"][2], D[k][0])
for k in ("ins", "gisb", "trf", "trench"):
    link(D["lay"][2], D[k][0])
for k in ("est", "ctrlb", "gisb", "trf", "trench"):
    link(D["svy"][2], D[k][0])
link(D["sld"][2], D["est"][0])
link(D["est"][2], D["earth"][0])
for k in ("pcp", "scada", "aux"):
    link(D["pcph"][2], D[k][0])
link(D["pcp"][2], D["schem"][0])
link(D["schem"][2], D["relay"][0])
for k, _, _ in DOCS["CVL"]:
    link(D[k][2], MS["civ_ifc"])
for k, _, _ in DOCS["PRI"] + DOCS["SEC"]:
    link(D[k][2], MS["ele_ifc"])

# procurement: RFQ, evaluation, order, manufacturer drawings, manufacturing, factory test, delivery
PKG = [("GIS225", "PRC.HV", "225 kV GIS", "gis225", 120, 30), ("GIS63", "PRC.HV", "63 kV GIS", "gis63", 100, 25),
       ("TR", "PRC.HV", "Power transformers 225/63 kV", "tr", 150, 35),
       ("HVC", "PRC.HV", "225 kV cables and accessories", "hvc", 60, 25),
       ("STL", "PRC.HV", "Steel structures and gantries", "lay", 50, 10),
       ("MVS", "PRC.MV", "MV switchgear", "mvs", 60, 15), ("AUX", "PRC.MV", "Auxiliary transformers", "mvs", 45, 15),
       ("LVB", "PRC.MV", "LV AC/DC boards, batteries and chargers", "aux", 40, 10),
       ("PCP", "PRC.SEC", "Protection and control panels", "pcp", 70, 15),
       ("SCA", "PRC.SEC", "SCADA and telecom equipment", "scada", 60, 15)]
P = {}  # package -> {step: code}
for pk, w, title, spec, mfg, ship in PKG:
    n, P[pk] = 1000, {}
    wbs(f"{w}.{pk}", w, pk, title)
    for step, name, days, res in (
            ("rfq", "issue request for quotation", 10, [("DM-BUYER", 1)]),
            ("eval", "technical and commercial evaluation", 15, [("DM-BUYER", 1), ("DM-REVIEW", 0.5)]),
            ("po", "award purchase order", 5, [("DM-BUYER", 1)]),
            ("dwg", "manufacturer drawings approval", 20, [("DM-REVIEW", 0.5)]),
            ("mfg", "fabrication and galvanizing" if pk == "STL" else "manufacturing", mfg, []),
            ("fat", "shop inspection" if pk == "STL" else "factory acceptance test", 5, [("DM-QAQC", 1)]),
            ("del", "shipping and delivery to site", ship, [])):
        P[pk][step] = act(f"PRC-{pk}-{n}", f"{title} - {name}", f"{w}.{pk}", days, res)
        n += 10
    steps = list(P[pk].values())
    for a, b in zip(steps, steps[1:], strict=False):
        link(a, b)
    link(D[spec][2], P[pk]["rfq"])
    link(pm["proc"], P[pk]["rfq"])
wbs("PRC.MV.BLK", "PRC.MV", "BLK", "Earthing and bulk electrical materials")
P["BLK"] = {step: act(f"PRC-BLK-{1000 + 10 * i}", f"Earthing and bulk materials - {name}", "PRC.MV.BLK", days, res)
            for i, (step, name, days, res) in enumerate((
                ("rfq", "issue request for quotation", 10, [("DM-BUYER", 1)]),
                ("po", "award purchase order", 5, [("DM-BUYER", 1)]), ("mfg", "supply", 20, []),
                ("del", "delivery to site", 5, [])))}
blk = list(P["BLK"].values())
for a, b in zip(blk, blk[1:], strict=False):
    link(a, b)
link(D["est"][2], P["BLK"]["rfq"])  # quantities from the earthing study
link(pm["proc"], P["BLK"]["rfq"])
link(P["GIS225"]["dwg"], D["gisb"][2])  # the building design needs the GIS loads
link(P["TR"]["dwg"], D["trf"][2])
link(P["PCP"]["dwg"], D["schem"][2])
link(P["GIS225"]["del"], MS["gis_del"])
link(P["TR"]["del"], MS["tr_del"])

# construction
SITE, FDN = [("DM-LABOR", 6), ("DM-SUPV", 1)], [("DM-CIVIL", 8), ("DM-SUPV", 1)]
BLDG = [("DM-BUILD", 10), ("DM-SUPV", 1)]
EM, CBL = [("DM-ELEC", 6), ("DM-SUPV", 1)], [("DM-ELEC", 8), ("DM-SUPV2", 1)]
LIFT = EM + [("DM-CRANE", 1)]  # heavy lifts
C = {}
for w, prefix, items in (
        ("CON.CIV.SITE", "CON-SITE", (("mob", "Mobilization and site establishment", 15, SITE),
                                      ("grade", "Site clearance and grading", 20, SITE),
                                      ("fence", "Access road and perimeter fencing", 25, SITE),
                                      ("earth", "Earthing grid installation", 20, SITE),
                                      ("drain", "Drainage and oil separator", 15, SITE))),
        ("CON.CIV.FDN", "CON-FDN", (("gisf", "GIS building foundations", 25, FDN),
                                    ("ctlf", "Control building foundations", 20, FDN),
                                    ("tr1f", "Transformer TR1 foundation and oil pit", 20, FDN),
                                    ("tr2f", "Transformer TR2 foundation and oil pit", 20, FDN),
                                    ("gantf", "Gantry and outdoor equipment foundations", 25, FDN),
                                    ("trench", "Cable trench construction", 30, FDN),
                                    ("wall", "Transformer firewall", 15, FDN))),
        ("CON.CIV.BLD", "CON-BLD", (("gisb", "GIS building structure", 40, BLDG),
                                    ("gisr", "GIS building roofing and cladding", 20, BLDG),
                                    ("gisfin", "GIS building finishing and overhead crane", 20, BLDG),
                                    ("ctlb", "Control building structure", 35, BLDG),
                                    ("ctlfin", "Control building finishing", 25, BLDG),
                                    ("serv", "Building services (HVAC, lighting, fire detection)", 25, BLDG),
                                    ("roads", "Internal roads and gravel surfacing", 20, SITE))),
        ("CON.EM.HV", "CON-HV", (("gant", "Gantry steel structures and busbars", 20, LIFT),
                                 ("gis225", "225 kV GIS erection", 35, LIFT),
                                 ("gas", "225 kV GIS gas filling and checks", 10, EM),
                                 ("gis63", "63 kV GIS erection", 30, EM),
                                 ("tr1", "Power transformer TR1 installation and oil filling", 20, LIFT),
                                 ("tr2", "Power transformer TR2 installation and oil filling", 20, LIFT),
                                 ("hvc", "225 kV cable laying", 25, EM),
                                 ("hvt", "225 kV cable terminations", 15, EM))),
        ("CON.EM.SEC", "CON-SEC", (("mvs", "MV switchgear installation", 15, EM),
                                   ("aux", "Auxiliary transformer installation", 8, EM),
                                   ("lvb", "LV AC/DC boards, batteries and chargers installation", 10, EM),
                                   ("pcp", "Protection and control panel installation", 15, EM),
                                   ("scada", "SCADA and telecom installation", 15, EM),
                                   ("fire", "Transformer firefighting system", 15, EM),
                                   ("cctv", "Perimeter lighting and CCTV", 15, EM))),
        ("CON.CBL", "CON-CBL", (("ctl", "Control cable laying", 35, CBL),
                                ("term", "Control cable terminations", 30, CBL),
                                ("lv", "LV power cabling", 20, CBL),
                                ("eq", "Earthing connections to equipment", 15, CBL),
                                ("light", "Lighting and small power", 20, CBL)))):
    for i, (k, name, days, res) in enumerate(items):
        C[k] = act(f"{prefix}-{1000 + 10 * i}", name, w, days, res)
for code in MATERIAL_QTY:
    ACTS[code]["res"].append(("DM-CABLE", 0))
for a, b, *rest in (
        (pm["hse"], C["mob"]), (pm["qp"], C["mob"]), (pm["pep"], C["mob"]), (C["mob"], C["grade"]),
        (D["svy"][2], C["grade"]), (C["grade"], C["fence"], "PR_SS", 5), (C["grade"], C["earth"]),
        (D["earth"][2], C["earth"]), (P["BLK"]["del"], C["earth"]), (C["grade"], C["drain"]),
        (D["trench"][2], C["drain"]),
        (C["earth"], C["gisf"], "PR_SS", 10), (D["gisb"][2], C["gisf"]), (C["earth"], C["ctlf"], "PR_SS", 10),
        (D["ctrlb"][2], C["ctlf"]), (C["earth"], C["tr1f"], "PR_SS", 10), (D["trf"][2], C["tr1f"]),
        (C["tr1f"], C["tr2f"], "PR_SS", 10), (C["earth"], C["gantf"], "PR_SS", 10), (D["trf"][2], C["gantf"]),
        (C["earth"], C["trench"], "PR_SS", 10), (D["trench"][2], C["trench"]),
        (C["tr1f"], C["wall"]), (C["tr2f"], C["wall"]),
        (C["gisf"], C["gisb"], "PR_FS", 5), (C["gisb"], C["gisr"]), (C["gisr"], C["gisfin"]),
        (C["ctlf"], C["ctlb"], "PR_FS", 5), (C["ctlb"], C["ctlfin"]), (C["gisfin"], C["serv"]),
        (C["ctlfin"], C["serv"], "PR_SS", 10), (C["ctlfin"], C["serv"], "PR_FF", 5),
        (C["trench"], C["roads"]), (C["gantf"], C["roads"]), (C["drain"], C["roads"]),
        (C["gantf"], C["gant"]), (P["STL"]["del"], C["gant"]), (C["gisfin"], C["gis225"]), (MS["gis_del"], C["gis225"]),
        (C["gis225"], C["gas"]), (C["gisfin"], C["gis63"]), (P["GIS63"]["del"], C["gis63"]),
        (C["gis225"], C["gis63"], "PR_SS", 10), (C["tr1f"], C["tr1"]), (C["wall"], C["tr1"]), (MS["tr_del"], C["tr1"]),
        (C["tr1"], C["tr2"], "PR_SS", 10), (C["tr2f"], C["tr2"]), (C["trench"], C["hvc"]), (P["HVC"]["del"], C["hvc"]),
        (C["hvc"], C["hvt"]), (C["gas"], C["hvt"]),
        (C["ctlfin"], C["mvs"]), (P["MVS"]["del"], C["mvs"]), (C["gantf"], C["aux"]), (P["AUX"]["del"], C["aux"]),
        (C["ctlfin"], C["lvb"]), (P["LVB"]["del"], C["lvb"]), (C["ctlfin"], C["pcp"]), (P["PCP"]["del"], C["pcp"]),
        (C["pcp"], C["scada"], "PR_SS", 5), (P["SCA"]["del"], C["scada"]), (C["tr2"], C["fire"]),
        (C["fence"], C["cctv"]), (C["roads"], C["cctv"]),
        (C["trench"], C["ctl"]), (C["pcp"], C["ctl"], "PR_SS", 5), (C["gis225"], C["ctl"], "PR_SS", 10),
        (C["ctl"], C["term"], "PR_SS", 10), (C["ctl"], C["term"], "PR_FF", 5), (C["lvb"], C["lv"]),
        (C["trench"], C["lv"]),
        (C["gant"], C["eq"]), (C["gas"], C["eq"]), (C["tr1"], C["eq"]), (C["tr2"], C["eq"]),
        (C["serv"], C["light"], "PR_SS", 5)):
    link(a, b, *rest)
for k in ("earth", "gisfin", "ctlfin", "roads", "fence", "wall"):
    link(C[k], MS["civ_done"])
link(MS["civ_ifc"], MS["civ_done"])
for k in ("gis63", "tr2", "hvt", "mvs", "aux", "pcp", "scada", "fire", "cctv", "term", "lv", "eq", "light", "serv"):
    link(C[k], MS["mc"])
link(MS["civ_done"], MS["mc"])
link(MS["ele_ifc"], MS["mc"])

# commissioning
OPT = [("DM-COMTEC", 3), ("DM-COMLEAD", 1)]
K = {}
for w, prefix, items in (
        ("COM.PRE", "COM-PRE", (("t225", "225 kV GIS high-voltage test", 5), ("t63", "63 kV GIS high-voltage test", 5),
                                ("ttr1", "Power transformer TR1 tests", 5), ("ttr2", "Power transformer TR2 tests", 5),
                                ("thvc", "225 kV cable sheath and high-voltage tests", 4),
                                ("prot", "Protection relay secondary injection tests", 15),
                                ("intl", "Control and interlocking functional tests", 15),
                                ("p2p", "SCADA point-to-point tests", 15),
                                ("tele", "Telecom and teleprotection tests", 8),
                                ("auxt", "Auxiliary systems tests (AC/DC, batteries)", 8),
                                ("firet", "Fire protection system tests", 5),
                                ("eres", "Earthing resistance measurements", 3), ("meter", "Metering tests", 5))),
        ("COM.ENR", "COM-ENR", (("proc", "Energization procedure approval", 10),
                                ("back", "Back-energization of the 225 kV GIS", 2),
                                ("etr1", "Energization of transformer TR1", 1),
                                ("etr2", "Energization of transformer TR2", 1),
                                ("load", "On-load tests and protection stability checks", 5),
                                ("trial", "Trial operation", 20))),
        ("COM.HO", "COM-HO", (("punch", "Punch list closure", 15), ("asb", "As-built documentation", 20),
                              ("train", "Operator training", 10), ("pai", "Provisional acceptance inspection", 5)))):
    for i, (k, name, days) in enumerate(items):
        K[k] = act(f"{prefix}-{1000 + 10 * i}", name, w, days, [("DM-REVIEW", 1)] if k in ("proc", "asb") else OPT)
for a, b, *rest in (
        (C["gas"], K["t225"]), (C["gis63"], K["t63"]), (C["tr1"], K["ttr1"]), (C["eq"], K["ttr1"]),
        (C["tr2"], K["ttr2"]),
        (K["ttr1"], K["ttr2"]), (C["hvt"], K["thvc"]), (C["term"], K["prot"]), (D["relay"][2], K["prot"]),
        (K["prot"], K["intl"], "PR_SS", 5), (K["prot"], K["intl"], "PR_FF", 5), (C["scada"], K["p2p"]),
        (K["intl"], K["p2p"], "PR_SS", 5), (C["scada"], K["tele"]), (C["lvb"], K["auxt"]), (C["lv"], K["auxt"]),
        (C["fire"], K["firet"]), (C["serv"], K["firet"]), (C["eq"], K["eres"]), (K["prot"], K["meter"]),
        (D["relay"][2], K["proc"]), (MS["mc"], K["proc"], "PR_SS", 0),
        (MS["rfe"], K["back"]), (K["back"], MS["energ"]), (MS["energ"], K["etr1"]), (K["etr1"], K["etr2"]),
        (K["etr2"], K["load"]), (K["load"], K["trial"]), (K["load"], K["punch"]), (K["punch"], K["asb"], "PR_SS", 5),
        (K["load"], K["train"]), (K["trial"], K["pai"]), (K["punch"], K["pai"]), (K["pai"], MS["pa"]),
        (MS["pa"], MS["end"]), (K["asb"], MS["end"]), (K["train"], MS["end"])):
    link(a, b, *rest)
for k in ("t225", "t63", "ttr2", "thvc", "intl", "p2p", "tele", "auxt", "firet", "eres", "meter", "proc"):
    link(K[k], MS["rfe"])
link(MS["mc"], MS["rfe"])

CODES = list(ACTS)
TID = {c: i + 1 for i, c in enumerate(CODES)}

# ---------------------------------------------------------------- calendar and engine
SERIAL0 = date(1899, 12, 30)
DAYS_XML = "".join(
    f"(0||{n}()())" if n in (1, 7) else f"(0||{n}()((0||0(s|08:00|f|12:00)())(0||1(s|13:00|f|17:00)())))"
    for n in range(1, 8))
EXC_XML = "".join(f"(0||{i}(d|{(date.fromisoformat(h) - SERIAL0).days})())" for i, h in enumerate(HOLIDAYS))
CLNDR_DATA = (f"(0||CalendarData()((0||DaysOfWeek()({DAYS_XML}))(0||VIEW(ShowTotal|Y)())"
              f"(0||Exceptions()({EXC_XML}))))")
ENV_KEYS = ("P6_DB_PATH", "P6_USER", "P6_BACKUP_DIR", "P6_LOG_DIR", "P6MCP_TOOL_PROFILE", "P6MCP_ALLOW_WRITES")


def server():
    """p6mcp.server. Imported here first, it gets a throwaway config file: never the user's settings."""
    if "p6mcp.server" not in sys.modules:
        if not os.environ.get("P6MCP_CONFIG"):
            os.environ["P6MCP_CONFIG"] = os.path.join(tempfile.mkdtemp(prefix="p6mcp_demo_"), "config.json")
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["P6_SKIP_PROCESS_CHECK"] = "1"
    import p6mcp.server as s
    return s


_CAL = []


def cal():
    if not _CAL:
        s = server()
        week, exc = s._cal_parse(CLNDR_DATA)
        _CAL.append(s._Cal(CAL_ID, CAL_NAME, HPD, week, dict(exc)))
    return _CAL[0]


def engine(dd, must, rem_h, status=None, actual=None, cstr=None):
    """Run the server's CPM engine on the network in memory: ({code: {es, ef, ls, lf, tf_h}}, engine)."""
    s, cl = server(), cal()
    cpm = object.__new__(s._Cpm)
    cpm.pid, cpm.dd, cpm.must, cpm.opts = 1, dd, must, dict(OPTS)
    cpm.prj = {"proj_short_name": PROJ, "last_recalc_date": dd, "plan_end_date": must, "scd_end_date": None,
               "clndr_id": CAL_ID, "critical_drtn_hr_cnt": 0}
    cpm.rule, cpm.cals, cpm.pcal = OPTS["sched_calendar_on_relationship_lag"], {CAL_ID: cl}, cl
    cpm.t = {}
    for c in CODES:
        a = ACTS[c]
        st = (status or {}).get(c, "TK_NotStart")
        ad = (actual or {}).get(c, (None, None))
        ct = (cstr or {}).get(c)
        cpm.t[TID[c]] = {"task_id": TID[c], "task_code": c, "task_name": a["name"], "proj_id": 1, "wbs_id": a["wbs"],
                         "task_type": a["type"], "status_code": st, "clndr_id": CAL_ID, "cal": cl, "hpd": HPD,
                         "rem": rem_h.get(c, 0.0), "act_start_date": ad[0], "act_end_date": ad[1],
                         "cstr_type": ct and "CS_MSOA", "cstr_date": ct, "cstr_type2": None, "cstr_date2": None,
                         "expect_end_date": None, "suspend_date": None, "resume_date": None,
                         "early_start_date": None, "early_end_date": None, "late_start_date": None,
                         "late_end_date": None, "total_float_hr_cnt": None, "remain_drtn_hr_cnt": rem_h.get(c, 0.0)}
    cpm.code = {c: TID[c] for c in CODES}
    cpm.links = [{"id": i + 1, "succ": TID[q], "pred": TID[p], "type": t, "lag": lag * HPD}
                 for i, (p, q, t, lag) in enumerate(LINKS)]
    cpm.ext_pred, cpm.ext_succ = [], []
    res = cpm.run()
    return {c: res[TID[c]] for c in CODES}, cpm


def wh(a, b):
    return cal().work_hours(a, b)


def check_network():
    preds, succs = {c: 0 for c in CODES}, {c: 0 for c in CODES}
    seen = set()
    for p, q, t, _lag in LINKS:
        assert p in ACTS and q in ACTS and p != q, (p, q)
        assert (p, q, t) not in seen, ("duplicate link", p, q, t)
        seen.add((p, q, t))
        succs[p] += 1
        preds[q] += 1
    nopred = [c for c in CODES if not preds[c]]
    nosucc = [c for c in CODES if not succs[c]]
    assert nopred == [MS["ntp"]], nopred
    assert nosucc == [MS["end"]], nosucc


# ---------------------------------------------------------------- schedules
def schedules():
    check_network()
    rnd = random.Random(225)
    plan_h = {c: ACTS[c]["days"] * HPD for c in CODES}
    # 1. baseline: nothing started, data date = start; the must finish date = the baseline finish
    base, _ = engine(START, None, plan_h)
    must = base[MS["end"]]["ef"]
    base, _ = engine(START, must, plan_h)
    # 2. as performed: longer durations and late starts give the actual dates before the data date
    factor = {}
    for c in CODES:
        if ACTS[c]["type"] != "TT_Task":
            continue
        r = rnd.random()
        factor[c] = 1.0 if r < 0.62 else rnd.uniform(1.1, 1.35) if r < 0.85 else rnd.uniform(1.35, 1.7) if r < 0.95 \
            else rnd.uniform(0.8, 0.95)
    late_start = {C["mob"]: 12, D["svy"][1]: 6, P["TR"]["rfq"]: 8, C["gisf"]: 10}  # working days after the plan
    factor.update({D["svy"][1]: 1.8, P["TR"]["dwg"]: 1.3, P["TR"]["mfg"]: 1.12, P["GIS225"]["mfg"]: 1.1,
                   C["earth"]: 1.4, C["gisf"]: 1.3})
    perf_h = {c: round(plan_h[c] * factor.get(c, 1.0) / HPD) * HPD for c in CODES}
    cstr = {c: cal().add_work_hours(base[c]["es"], d * HPD) for c, d in late_start.items()}
    perf, _ = engine(START, must, perf_h, cstr=cstr)
    # 3. the update at the data date
    status, actual, rem_h, pct = {}, {}, {}, {}
    for c in CODES:
        a, x = ACTS[c], perf[c]
        done = x["es"] < DD if a["type"] == "TT_Mile" else x["ef"] <= DD
        if done:
            status[c] = "TK_Complete"
            actual[c] = (x["es"], x["ef"]) if a["type"] == "TT_Task" else ((x["es"],) * 2 if a["type"] == "TT_Mile"
                                                                            else (x["ef"],) * 2)
            rem_h[c], pct[c] = 0.0, 100
        elif a["type"] == "TT_Task" and x["es"] < DD:
            status[c] = "TK_Active"
            actual[c] = (x["es"], None)
            rem_h[c] = wh(DD, x["ef"])
            done_h = wh(x["es"], DD)
            pct[c] = min(95, max(5, round(100 * done_h / (done_h + rem_h[c]) + rnd.uniform(-6, 6))))
        else:
            status[c] = "TK_NotStart"
            rem_h[c], pct[c] = plan_h[c], 0
    if status[P["GIS63"]["mfg"]] == "TK_NotStart":  # a re-estimate known at the data date
        rem_h[P["GIS63"]["mfg"]] = plan_h[P["GIS63"]["mfg"]] + 10 * HPD
    cur, cpm = engine(DD, must, rem_h, status, actual)
    return {"base": base, "cur": cur, "cpm": cpm, "must": must, "plan_h": plan_h, "perf": perf, "status": status,
            "actual": actual, "rem_h": rem_h, "pct": pct}


def gaps(res, status, cpm):
    """(pred, succ, type) -> working hours from the link's anchor (predecessor date plus lag) to the
    successor date, for links whose predecessor is open."""
    s, cl = server(), cal()
    out = {}
    for p, q, t, lag in LINKS:
        if status.get(p) == "TK_Complete":
            continue
        a = res[p]["ef"] if s.LINK_FROM[t] == "f" else res[p]["es"]
        b = cpm.shift(cl, a, cpm.eff_lag({"type": t, "lag": lag * HPD}, cpm.t[TID[p]], cl))
        out[(p, q, t)] = wh(b, res[q]["es"] if s.LINK_TO[t] == "s" else res[q]["ef"])
    return out


def free_float(res, status, gap, must):
    """Free float in hours of the open activities: the smallest gap to a successor (else to the must
    finish date), as P6 shows it for finish float."""
    out = {}
    for c in CODES:
        if status.get(c, "TK_NotStart") == "TK_Complete":
            continue
        g = [v for (p, _q, _t), v in gap.items() if p == c]
        out[c] = min(g) if g else wh(res[c]["ef"], must)
    return out


def float_paths(res, status, gap):
    """{code: (float_path, float_path_order)} of the open activities on the first FLOAT_PATHS paths
    (total float mode): path 1 traces back from the project finish through links without a gap; each
    next path starts at the open activity off the earlier paths with the least total float (latest
    early finish, then Activity ID) and traces back the same way. Order on a path = early start."""
    drive = {}
    for (p, q, _t), v in gap.items():
        if abs(v) < 0.01:
            drive.setdefault(q, []).append(p)
    opened = [c for c in CODES if status.get(c, "TK_NotStart") != "TK_Complete"]
    out, used = {}, set()
    for k in range(1, FLOAT_PATHS + 1):
        left = [c for c in opened if c not in used]
        if not left:
            break
        seed = MS["end"] if k == 1 else min(left, key=lambda c: (res[c]["tf_h"], -res[c]["ef"].timestamp(), c))
        members, todo = set(), [seed]
        while todo:
            c = todo.pop()
            if c in members or c in used:
                continue
            members.add(c)
            todo += drive.get(c, [])
        for i, c in enumerate(sorted(members, key=lambda c: (res[c]["es"], res[c]["ef"], c)), 1):
            out[c] = (k, i)
        used |= members
    return out


# ---------------------------------------------------------------- XER rows
FIELDS = {
    "OBS": ["obs_id", "parent_obs_id", "guid", "seq_num", "obs_name", "obs_descr"],
    "PROJECT": ["proj_id", "project_flag", "name_sep_char", "def_complete_pct_type", "proj_short_name",
                "orig_proj_id", "clndr_id", "sum_base_proj_id", "task_code_base", "task_code_step",
                "critical_drtn_hr_cnt", "last_recalc_date", "plan_start_date", "plan_end_date", "scd_end_date",
                "add_date", "def_duration_type", "task_code_prefix", "guid", "def_task_type", "critical_path_type",
                "rem_target_link_flag", "export_flag", "last_schedule_date"],
    "CALENDAR": ["clndr_id", "default_flag", "clndr_name", "proj_id", "base_clndr_id", "last_chng_date", "clndr_type",
                 "day_hr_cnt", "week_hr_cnt", "month_hr_cnt", "year_hr_cnt", "rsrc_private", "clndr_data"],
    "PROJWBS": ["wbs_id", "proj_id", "obs_id", "seq_num", "est_wt", "proj_node_flag", "sum_data_flag", "status_code",
                "wbs_short_name", "wbs_name", "parent_wbs_id", "ev_compute_type", "ev_etc_compute_type", "guid"],
    "RSRC": ["rsrc_id", "parent_rsrc_id", "clndr_id", "rsrc_seq_num", "rsrc_name", "rsrc_short_name", "def_qty_per_hr",
             "cost_qty_type", "active_flag", "auto_compute_act_flag", "def_cost_qty_link_flag", "ot_flag", "rsrc_type"],
    "TASK": ["task_id", "proj_id", "wbs_id", "clndr_id", "phys_complete_pct", "rev_fdbk_flag", "est_wt",
             "lock_plan_flag", "auto_compute_act_flag", "complete_pct_type", "task_type", "duration_type",
             "status_code", "task_code", "task_name", "rsrc_id", "total_float_hr_cnt", "free_float_hr_cnt",
             "remain_drtn_hr_cnt", "act_work_qty", "remain_work_qty", "target_work_qty", "target_drtn_hr_cnt",
             "target_equip_qty", "act_equip_qty", "remain_equip_qty", "cstr_date", "act_start_date", "act_end_date",
             "late_start_date", "late_end_date", "expect_end_date", "early_start_date", "early_end_date",
             "restart_date", "reend_date", "target_start_date", "target_end_date", "rem_late_start_date",
             "rem_late_end_date", "cstr_type", "priority_type", "suspend_date", "resume_date", "float_path",
             "float_path_order", "guid", "cstr_date2", "cstr_type2", "driving_path_flag"],
    "TASKPRED": ["task_pred_id", "task_id", "pred_task_id", "proj_id", "pred_proj_id", "pred_type", "lag_hr_cnt"],
    "TASKRSRC": ["taskrsrc_id", "task_id", "proj_id", "cost_qty_link_flag", "role_id", "rsrc_id", "remain_qty",
                 "target_qty", "remain_qty_per_hr", "target_qty_per_hr", "act_ot_qty", "act_reg_qty", "cost_per_qty",
                 "target_cost", "act_reg_cost", "act_ot_cost", "remain_cost", "act_start_date", "act_end_date",
                 "restart_date", "reend_date", "target_start_date", "target_end_date", "rem_late_start_date",
                 "rem_late_end_date", "guid", "rsrc_type"],
}
# P6 columns of the demo that make_db's SCHEMA leaves out (added to the demo database before the import)
EXTRA_COLUMNS = {
    "PROJECT": [("rem_target_link_flag", "TEXT")],
    "TASK": [("duration_type", "TEXT"), ("target_work_qty", "REAL"), ("act_work_qty", "REAL"),
             ("remain_work_qty", "REAL"), ("target_equip_qty", "REAL"), ("act_equip_qty", "REAL"),
             ("remain_equip_qty", "REAL"), ("rem_late_start_date", "TEXT"), ("rem_late_end_date", "TEXT")],
    "TASKRSRC": [("cost_per_qty", "REAL"), ("target_qty_per_hr", "REAL"), ("remain_qty_per_hr", "REAL"),
                 ("rem_late_start_date", "TEXT"), ("rem_late_end_date", "TEXT")],
    "RSRC": [("def_cost_qty_link_flag", "TEXT"), ("cost_qty_type", "TEXT"), ("active_flag", "TEXT")],
}


def fmt(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, float):
        return server()._xer_encode(round(v, 4))
    return v


def rows_of(table, values):
    return [{f: fmt(v.get(f)) for f in FIELDS[table]} for v in values]


def project_tables(sch, baseline: bool) -> dict:
    res = sch["base"] if baseline else sch["cur"]
    status = {} if baseline else sch["status"]
    actual = {} if baseline else sch["actual"]
    rem_h = sch["plan_h"] if baseline else sch["rem_h"]
    pct = {} if baseline else sch["pct"]
    dd = START if baseline else DD
    cpm = engine(dd, sch["must"], rem_h, status or None, actual or None)[1]
    gap = gaps(res, status, cpm)
    ff = free_float(res, status, gap, sch["must"])
    paths = {} if baseline else float_paths(res, status, gap)
    drive = {c for c, (k, _i) in paths.items() if k == 1}
    finish = max(r["ef"] for r in res.values())
    t = {"OBS": rows_of("OBS", [{"obs_id": 1, "seq_num": 0, "obs_name": "Enterprise"}])}
    t["PROJECT"] = rows_of("PROJECT", [{
        "proj_id": 1, "project_flag": "Y", "name_sep_char": ".", "def_complete_pct_type": "CP_Phys",
        "proj_short_name": PROJ_BL if baseline else PROJ, "clndr_id": CAL_ID, "task_code_base": "1000",
        "task_code_step": "10", "critical_drtn_hr_cnt": 0.0, "last_recalc_date": dd, "plan_start_date": START,
        "plan_end_date": sch["must"], "scd_end_date": finish, "add_date": START, "def_duration_type": "DT_FixedDUR2",
        "task_code_prefix": "A", "def_task_type": "TT_Task", "critical_path_type": "CT_TotFloat",
        "rem_target_link_flag": "Y", "export_flag": "Y",
        "last_schedule_date": START + timedelta(hours=1) if baseline else LAST_F9}])
    t["CALENDAR"] = rows_of("CALENDAR", [{
        "clndr_id": CAL_ID, "default_flag": "N", "clndr_name": CAL_NAME, "proj_id": 1,
        "last_chng_date": "2026-01-15 00:00", "clndr_type": "CA_Project", "day_hr_cnt": 8.0, "week_hr_cnt": 40.0,
        "month_hr_cnt": 172.0, "year_hr_cnt": 2000.0, "rsrc_private": "N", "clndr_data": CLNDR_DATA}])
    so = dict(OPTS, schedoptions_id="1", proj_id="1", level_float_thrs_cnt="0", level_outer_assign_flag="N",
              level_outer_assign_priority="1", level_over_alloc_pct="0", level_within_float_flag="N",
              level_keep_sched_date_flag="Y", level_all_rsrc_flag="Y", enable_multiple_longest_path_calc="Y",
              limit_multiple_longest_path_calc="Y", max_multiple_longest_path=str(FLOAT_PATHS),
              use_total_float_multiple_longest_paths="Y")
    t["SCHEDOPTIONS"] = [{f: so.get(f) for f in server().SCHED_XER_COLS}]
    wid = {k: i + 1 for i, (k, *_r) in enumerate(WBS)}
    t["PROJWBS"] = rows_of("PROJWBS", [
        {"wbs_id": wid[k], "proj_id": 1, "obs_id": 1, "seq_num": 10 * (i + 1), "est_wt": 1,
         "proj_node_flag": "Y" if par is None else "N", "sum_data_flag": "N", "status_code": "WS_Open",
         "wbs_short_name": PROJ_BL if par is None and baseline else short, "wbs_name": name,
         "parent_wbs_id": wid.get(par), "ev_compute_type": "EC_Cmp_pct", "ev_etc_compute_type": "EE_Rem_hr"}
        for i, (k, par, short, name) in enumerate(WBS)])
    rid = {r: i + 1 for i, r in enumerate(RES)}
    t["RSRC"] = rows_of("RSRC", [{"rsrc_id": rid[r], "rsrc_seq_num": rid[r], "rsrc_short_name": r, "rsrc_name": nm,
                                  "rsrc_type": typ, "active_flag": "Y", "def_cost_qty_link_flag": "Y",
                                  "cost_qty_type": "QT_Hour", "def_qty_per_hr": 1.0, "ot_flag": "N",
                                  "auto_compute_act_flag": "Y"} for r, (nm, typ, _rate) in RES.items()])
    tasks, asgs = [], []
    rnd = random.Random(63 if baseline else 64)
    for c in CODES:
        a, x = ACTS[c], res[c]
        st = status.get(c, "TK_NotStart")
        as_, af = actual.get(c, (None, None))
        is_open = st != "TK_Complete"
        bx = sch["base"][c]
        tgt = (bx["es"], bx["ef"]) if st != "TK_NotStart" else (x["es"], x["ef"])
        if a["type"] == "TT_Mile":
            tgt = (tgt[0], tgt[0])
        elif a["type"] == "TT_FinMile":
            tgt = (tgt[1], tgt[1])
        plan = sch["plan_h"][c]
        rem = rem_h[c] if is_open else 0.0
        done_h = wh(as_, af or DD) if as_ and a["type"] == "TT_Task" else 0.0
        work = {"work": [0.0, 0.0, 0.0], "equip": [0.0, 0.0, 0.0]}  # target, actual, remaining
        for r, people in a["res"]:
            typ, rate = RES[r][1], RES[r][2]
            target = MATERIAL_QTY[c] if typ == "RT_Mat" else people * plan
            if st == "TK_Complete":
                act_q, rem_q = target * rnd.uniform(0.92, 1.25) * (done_h / plan if plan else 1.0), 0.0
            elif st == "TK_Active":
                share = done_h / (done_h + rem) if done_h + rem else 0.0
                act_q = (target * share if typ == "RT_Mat" else people * done_h) * rnd.uniform(0.85, 1.15)
                rem_q = target * (1 - share) if typ == "RT_Mat" else people * rem
            else:
                act_q, rem_q = 0.0, target
            act_q, rem_q = round(act_q, 1), round(rem_q, 1)
            if typ != "RT_Mat":  # TASK units: labor in *_work_qty, nonlabor in *_equip_qty (material not rolled up)
                acc = work["work" if typ == "RT_Labor" else "equip"]
                acc[0] += target
                acc[1] += act_q
                acc[2] += rem_q
            per_hr = target / plan if plan else float(people)
            asgs.append({"taskrsrc_id": len(asgs) + 1, "task_id": TID[c], "proj_id": 1, "cost_qty_link_flag": "Y",
                         "rsrc_id": rid[r], "remain_qty": rem_q, "target_qty": target,
                         "remain_qty_per_hr": rem_q / rem if rem else per_hr, "target_qty_per_hr": per_hr,
                         "act_ot_qty": 0.0, "act_reg_qty": act_q, "cost_per_qty": float(rate),
                         "target_cost": target * rate, "act_reg_cost": act_q * rate, "act_ot_cost": 0.0,
                         "remain_cost": rem_q * rate, "act_start_date": as_, "act_end_date": af,
                         "restart_date": x["es"] if is_open else None, "reend_date": x["ef"] if is_open else None,
                         "target_start_date": tgt[0], "target_end_date": tgt[1],
                         "rem_late_start_date": x["ls"] if is_open else None,
                         "rem_late_end_date": x["lf"] if is_open else None, "rsrc_type": typ})
        ms = a["type"] in ("TT_Mile", "TT_FinMile")
        fp = paths.get(c, (None, None))
        tasks.append({
            "task_id": TID[c], "proj_id": 1, "wbs_id": wid[a["wbs"]], "clndr_id": CAL_ID,
            "phys_complete_pct": float(pct.get(c, 0)), "rev_fdbk_flag": "N", "est_wt": 1, "lock_plan_flag": "N",
            "auto_compute_act_flag": "N", "complete_pct_type": "CP_Phys", "task_type": a["type"],
            "duration_type": "DT_FixedDrtn" if ms else "DT_FixedDUR2", "status_code": st, "task_code": c,
            "task_name": a["name"], "rsrc_id": rid[a["res"][0][0]] if a["res"] else None,
            "total_float_hr_cnt": x["tf_h"] if is_open else None, "free_float_hr_cnt": ff.get(c) if is_open else None,
            "remain_drtn_hr_cnt": rem, "act_work_qty": round(work["work"][1], 1),
            "remain_work_qty": round(work["work"][2], 1), "target_work_qty": work["work"][0],
            "target_drtn_hr_cnt": plan, "target_equip_qty": work["equip"][0],
            "act_equip_qty": round(work["equip"][1], 1),
            "remain_equip_qty": round(work["equip"][2], 1), "act_start_date": as_, "act_end_date": af,
            "late_start_date": x["ls"], "late_end_date": x["lf"], "early_start_date": x["es"],
            "early_end_date": x["ef"],
            "restart_date": x["es"] if is_open else None, "reend_date": x["ef"] if is_open else None,
            "target_start_date": tgt[0], "target_end_date": tgt[1],
            "rem_late_start_date": x["ls"] if is_open else None, "rem_late_end_date": x["lf"] if is_open else None,
            "priority_type": "PT_Normal", "float_path": fp[0], "float_path_order": fp[1],
            "driving_path_flag": "Y" if c in drive else "N"})
    t["TASK"] = rows_of("TASK", tasks)
    t["TASKPRED"] = rows_of("TASKPRED", [
        {"task_pred_id": i + 1, "task_id": TID[q], "pred_task_id": TID[p], "proj_id": 1, "pred_proj_id": 1,
         "pred_type": typ, "lag_hr_cnt": lag * HPD} for i, (p, q, typ, lag) in enumerate(LINKS)])
    t["TASKRSRC"] = rows_of("TASKRSRC", asgs)
    return t


def write_xer(path, tables) -> None:
    s = server()
    head = ["ERMHDR", "24.12", DD.strftime("%Y-%m-%d"), "Project", "ADMIN", "ADMIN", "dbxDatabaseNoName",
            "Project Management", "USD"]
    lines = ["\t".join(head)]
    for name in s.XER_EXPORT_TABLES:
        if name not in tables:
            continue
        fields = s.SCHED_XER_COLS if name == "SCHEDOPTIONS" else FIELDS[name]
        lines.append("%T\t" + name)
        lines.append("%F\t" + "\t".join(fields))
        for r in tables[name]:
            lines.append("%R\t" + "\t".join(s._xer_encode(r.get(f)) for f in fields))
    lines.append("%E")
    with open(path, "w", encoding="cp1252", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")


def write_xers(out_dir: str) -> dict:
    """The two XER files in out_dir and a summary of the schedule."""
    sch = schedules()
    out = {}
    for baseline, name in ((True, PROJ_BL), (False, PROJ)):
        out[name] = os.path.join(out_dir, name + ".xer")
        write_xer(out[name], project_tables(sch, baseline))
    st, cur, base = sch["status"], sch["cur"], sch["base"]
    out.update({
        "activities": len(CODES), "links": len(LINKS), "wbs_nodes": len(WBS), "resources": len(RES),
        "status": {k: sum(1 for v in st.values() if v == k) for k in ("TK_Complete", "TK_Active", "TK_NotStart")},
        "baseline_finish": base[MS["end"]]["ef"].strftime("%Y-%m-%d %H:%M"),
        "forecast_finish": cur[MS["end"]]["ef"].strftime("%Y-%m-%d %H:%M"),
        "slip_wd": round(wh(base[MS["end"]]["ef"], cur[MS["end"]]["ef"]) / HPD, 1),
        "min_tf_d": round(min(r["tf_h"] for c, r in cur.items() if st[c] != "TK_Complete") / HPD, 1)})
    return out


# ---------------------------------------------------------------- the demo database
def use_db(path: str, work: str):
    """Point the server at the database path, full profile and writes allowed, backups and edit log in
    work (the config file is P6MCP_CONFIG: a throwaway one, see server())."""
    s = server()
    from p6mcp import config, schema
    cfg_path = os.environ.get("P6MCP_CONFIG")
    if not cfg_path:
        raise RuntimeError("P6MCP_CONFIG is not set: refusing to write the user's settings")
    con = sqlite3.connect(path)
    try:
        fp = schema.detect(con)["fingerprint"]
    finally:
        con.close()
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["P6_SKIP_PROCESS_CHECK"] = "1"
    config.save({"databases": [{"name": "demo", "kind": "sqlite", "path": path, "confirmed": True}],
                 "active_database": "demo", "tool_profile": "full", "allow_writes": True,
                 "backup_dir": os.path.join(work, "backups"), "edit_log_dir": os.path.join(work, "editlog"),
                 "backup_retention": 10, "trusted_schemas": [fp]}, cfg_path)
    s._refresh()
    return s


def build_db(path: str, work: str = None) -> dict:
    """make_db's database at path with DEMO-SS225 imported (the baseline as its project baseline);
    the XER files, backups and edit log go to work (default: the folder of path). Returns the
    write_xers summary."""
    path = os.path.abspath(path)
    work = os.path.abspath(work or os.path.dirname(path))
    os.makedirs(work, exist_ok=True)
    info = write_xers(work)
    make_db.build(path)
    con = sqlite3.connect(path)
    for table, cols in EXTRA_COLUMNS.items():
        for col, typ in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    con.commit()
    con.close()
    s = use_db(path, work)
    for kw in ({"path": info[PROJ], "mode": "new", "eps": EPS},
               {"path": info[PROJ_BL], "mode": "baseline", "project": PROJ, "set_as_project_baseline": True}):
        res = s.p6_import_xer(confirm=True, **kw)
        if res.get("applied") is False or res.get("would_refuse"):
            raise RuntimeError(f"demo import refused: {json.dumps(res, default=str)[:2000]}")
    return info


if __name__ == "__main__":
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    print(json.dumps(build_db(os.path.join(out, "demo.db"), out), indent=2))
