"""
rag_engine.py
=============
NeMo Retriever RAG Engine – IBC 2023 Knowledge Base

Replaces hardcoded Python compliance rules with live semantic search
against the full IBC 2023 text corpus.

Pipeline
--------
1. Embed query  : POST /v1/embeddings   → nvidia/nv-embedqa-e5-v5
2. Cosine search: numpy dot product over pre-embedded corpus chunks
3. Rerank       : POST /v1/ranking      → nvidia/nv-rerankqa-mistral-4b-v3
4. Inject       : top-K chunks injected into LLM compliance + manifest prompts

Fallback
--------
When no API key is present, keyword TF-IDF scoring is used so the
compliance engine still functions in development without any GPU credits.

IBC 2023 Corpus
---------------
Real section text covering the chapters most relevant to architectural
permit submissions. Extend by appending to IBC_CORPUS below.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NeMo Retriever endpoints
# ---------------------------------------------------------------------------

NIM_EMBED_ENDPOINT  = "https://integrate.api.nvidia.com/v1/embeddings"
NIM_RERANK_ENDPOINT = "https://integrate.api.nvidia.com/v1/ranking"
EMBED_MODEL         = "nvidia/nv-embedqa-e5-v5"
RERANK_MODEL        = "nvidia/nv-rerankqa-mistral-4b-v3"
EMBED_DIM           = 1024   # nv-embedqa-e5-v5 output dimension


# ---------------------------------------------------------------------------
# IBC 2023 Text Corpus
# ---------------------------------------------------------------------------

IBC_CORPUS: List[Dict[str, str]] = [
    # ── Chapter 3: Occupancy Classification ─────────────────────────────
    {
        "id": "IBC-302.1",
        "section": "IBC 2023 §302.1",
        "chapter": "Chapter 3 – Use and Occupancy",
        "text": (
            "IBC 2023 §302.1 GENERAL. Structures or portions of structures shall be classified "
            "with respect to occupancy in one or more of the groups listed in this section. "
            "A room or space that is intended to be occupied at different times for different "
            "purposes shall comply with all applicable requirements for each of the purposes "
            "for which the room or space will be occupied. Structures with multiple occupancies "
            "shall comply with Section 508. Where a structure is proposed for a purpose that "
            "is not specifically listed in this section, the structure shall be classified in "
            "the occupancy group it most nearly resembles, based on the fire safety and relative "
            "hazard of the occupancy."
        ),
    },
    {
        "id": "IBC-303",
        "section": "IBC 2023 §303",
        "chapter": "Chapter 3 – Use and Occupancy",
        "text": (
            "IBC 2023 §303 ASSEMBLY GROUP A. Assembly Group A occupancy includes, among others, "
            "the use of a building or structure, or a portion thereof, for the gathering of "
            "persons for purposes such as civic, social or religious functions; recreation, food "
            "or drink consumption; or awaiting transportation. "
            "A-1: assembly uses, usually with fixed seating, intended for the production and "
            "viewing of the performing arts. "
            "A-2: assembly uses intended for food and/or drink consumption including banquet halls, "
            "casinos, nightclubs, restaurants, taverns and bars. "
            "A-3: assembly uses intended for worship, recreation or amusement including amusement "
            "arcades, art galleries, bowling alleys, community halls, courtrooms, dance halls, "
            "exhibition halls, gymnasiums, indoor swimming pools, libraries, museums, pool and "
            "billiard halls, waiting areas."
        ),
    },
    {
        "id": "IBC-304",
        "section": "IBC 2023 §304",
        "chapter": "Chapter 3 – Use and Occupancy",
        "text": (
            "IBC 2023 §304 BUSINESS GROUP B. Business Group B occupancy includes, among others, "
            "the use of a building or structure, or a portion thereof, for office, professional "
            "or service-type transactions, including storage of records and accounts. Business "
            "occupancies shall include, but not be limited to, the following: Airport traffic "
            "control towers, ambulatory care facilities, animal hospitals and kennels, banks, "
            "barber and beauty shops, car wash, civic administration, clinic outpatient, "
            "dry cleaning and laundries, educational occupancies for students above the 12th grade, "
            "electronic data processing, fire stations, florists and nurseries, laboratories "
            "testing and research, laundromats, motor vehicle showrooms, post offices, print "
            "shops, professional services such as attorneys offices and engineering firms, "
            "radio and television stations, telephone exchanges, training and skill development."
        ),
    },
    {
        "id": "IBC-415",
        "section": "IBC 2023 §415",
        "chapter": "Chapter 4 – Special Detailed Requirements",
        "text": (
            "IBC 2023 §415 HIGH-HAZARD GROUP H. High-Hazard Group H occupancy includes, among "
            "others, the use of a building or structure, or a portion thereof, that involves "
            "the manufacturing, processing, generation or storage of materials that constitute "
            "a physical or health hazard. "
            "H-1: Buildings and structures containing materials that pose a detonation hazard. "
            "H-2: Buildings and structures containing materials that pose a deflagration hazard "
            "or a hazard from accelerated burning. "
            "H-3: Buildings and structures containing materials that readily support combustion "
            "or pose a physical hazard. "
            "SPRINKLER REQUIREMENT §415.9: H-1 occupancies shall be equipped with an automatic "
            "sprinkler system installed throughout. The sprinkler system shall be installed in "
            "accordance with Section 903.3.1.1. H-1 and H-2 occupancies are prohibited in "
            "Type V-B construction."
        ),
    },
    # ── Chapter 5: Height and Area ───────────────────────────────────────
    {
        "id": "IBC-504.3",
        "section": "IBC 2023 Table 504.3",
        "chapter": "Chapter 5 – Height and Area",
        "text": (
            "IBC 2023 TABLE 504.3 ALLOWABLE BUILDING HEIGHT IN FEET ABOVE GRADE PLANE. "
            "Type I-A: Unlimited. Type I-B: Unlimited. "
            "Type II-A: 65 feet. Type II-B: 55 feet. "
            "Type III-A: 65 feet. Type III-B: 55 feet. "
            "Type IV-HT: 85 feet. Type IV-A, IV-B, IV-C: Unlimited. "
            "Type V-A: 50 feet. Type V-B: 40 feet. "
            "Note: Heights may be increased by 20 feet for buildings equipped with automatic "
            "sprinkler systems per Section 504.3.1. For Business (B) occupancy, height "
            "increase of 20 feet permitted when fully sprinklered. "
            "For High-Hazard (H) occupancies, see Section 415.6 for special limitations."
        ),
    },
    {
        "id": "IBC-504.4",
        "section": "IBC 2023 Table 504.4",
        "chapter": "Chapter 5 – Height and Area",
        "text": (
            "IBC 2023 TABLE 504.4 ALLOWABLE NUMBER OF STORIES ABOVE GRADE PLANE. "
            "Type I-A and I-B: Unlimited stories. "
            "Type II-A: 4 stories (B occupancy, non-sprinklered); 5 stories with sprinklers. "
            "Type II-B: 4 stories (B occupancy). "
            "Type III-A: 4 stories. Type III-B: 3 stories. "
            "Type IV-HT: 5 stories. Type IV-A, IV-B, IV-C: Unlimited. "
            "Type V-A: 2 stories. Type V-B: 1 story. "
            "Exception: Story limits may be increased for buildings provided with sprinkler "
            "systems per Section 903.3.1.1. "
            "For Institutional Group I-2 occupancies, a maximum of one story shall be permitted "
            "in Type IIA, IIIA, or IV construction when not sprinklered."
        ),
    },
    {
        "id": "IBC-506",
        "section": "IBC 2023 §506",
        "chapter": "Chapter 5 – Height and Area",
        "text": (
            "IBC 2023 §506 BUILDING AREA MODIFICATIONS. The areas specified in Table 506.2 "
            "shall be permitted to be increased based on the installation of an approved "
            "automatic sprinkler system installed throughout the building. "
            "For a building equipped with an automatic sprinkler system throughout, the "
            "individual floor area shall be increased by a factor of 3 over the value in "
            "Table 506.2. Where a building is equipped with an automatic sprinkler system "
            "and is bounded by a public way or open space, additional increases are permitted."
        ),
    },
    # ── Chapter 6: Types of Construction ────────────────────────────────
    {
        "id": "IBC-601",
        "section": "IBC 2023 Table 601",
        "chapter": "Chapter 6 – Types of Construction",
        "text": (
            "IBC 2023 TABLE 601 FIRE-RESISTANCE RATING REQUIREMENTS FOR BUILDING ELEMENTS. "
            "Type I-A: Structural frame 3 hours, bearing walls exterior 3 hours, "
            "bearing walls interior 3 hours, floor construction 2 hours, roof construction 1.5 hours. "
            "Type I-B: Structural frame 2 hours, bearing walls 2 hours, floor 2 hours, roof 1 hour. "
            "Type II-A: Structural frame 1 hour, bearing walls 1 hour, floor 1 hour, roof 1 hour. "
            "Type II-B: Structural frame 0 hours, bearing walls 0 hours, floor 0 hours, roof 0 hours. "
            "Type V-A: Structural frame 1 hour throughout. "
            "Type V-B: No fire-resistance requirements. "
            "Note: For the purposes of Table 601, structural frame includes columns, girders, "
            "trusses, and beams that are part of the primary structural system."
        ),
    },
    # ── Chapter 9: Fire Protection ───────────────────────────────────────
    {
        "id": "IBC-903.2",
        "section": "IBC 2023 §903.2",
        "chapter": "Chapter 9 – Fire Protection",
        "text": (
            "IBC 2023 §903.2 WHERE REQUIRED. Approved automatic sprinkler systems in new "
            "buildings and structures shall be provided in the locations described in "
            "Sections 903.2.1 through 903.2.13. "
            "§903.2.1 Group A: An automatic sprinkler system shall be provided throughout "
            "stories containing a Group A occupancy and throughout all floors between the "
            "Group A occupancy and the level of exit discharge serving that occupancy where "
            "the fire area exceeds 12,000 square feet. "
            "§903.2.2 Group B: An automatic sprinkler system shall be provided throughout "
            "all buildings with a Group B fire area exceeding 12,000 square feet. "
            "§903.2.4 Group I: An automatic sprinkler system shall be provided throughout "
            "buildings with a Group I fire area. I-1, I-2, I-3, and I-4 occupancies require "
            "sprinkler systems regardless of area. "
            "§903.2.5 Group M: An automatic sprinkler system shall be provided throughout "
            "buildings with a Group M fire area exceeding 12,000 square feet."
        ),
    },
    {
        "id": "IBC-907.2",
        "section": "IBC 2023 §907.2",
        "chapter": "Chapter 9 – Fire Protection",
        "text": (
            "IBC 2023 §907.2 WHERE REQUIRED — NEW BUILDINGS AND STRUCTURES. "
            "An approved manual and automatic fire alarm system shall be provided in accordance "
            "with Sections 907.2.1 through 907.2.24. "
            "§907.2.1 Group A: A manual fire alarm system that initiates the occupant notification "
            "signal utilizing an emergency voice/alarm communication system shall be installed "
            "in Group A occupancies where the occupant load exceeds 300. "
            "§907.2.2 Group B: A manual fire alarm system shall be installed in Group B "
            "occupancies where the building has an occupant load of 500 or more persons or more "
            "than 100 persons above or below the lowest level of exit discharge. "
            "§907.2.6 Group I: A manual fire alarm system shall be installed in Group I "
            "occupancies. Group I-2 shall have automatic fire detection devices."
        ),
    },
    # ── Chapter 10: Means of Egress ──────────────────────────────────────
    {
        "id": "IBC-1004",
        "section": "IBC 2023 §1004",
        "chapter": "Chapter 10 – Means of Egress",
        "text": (
            "IBC 2023 §1004 OCCUPANT LOAD. The number of occupants shall be computed at the "
            "rate of one occupant per unit of area as prescribed in Table 1004.1. "
            "TABLE 1004.1 MAXIMUM FLOOR AREA ALLOWANCES PER OCCUPANT: "
            "Assembly without fixed seating, concentrated: 7 sq ft net per occupant. "
            "Assembly without fixed seating, unconcentrated: 15 sq ft net per occupant. "
            "Business areas: 150 sq ft gross per occupant. "
            "Educational: 20 sq ft net per occupant. "
            "Industrial: 100 sq ft gross per occupant. "
            "Mercantile: 60 sq ft gross per occupant. "
            "Residential: 200 sq ft gross per occupant. "
            "Storage: 300 sq ft gross per occupant. "
            "Where the occupant load is not provided in Table 1004.1, the occupant load shall "
            "be determined by the registered design professional."
        ),
    },
    {
        "id": "IBC-1006",
        "section": "IBC 2023 §1006",
        "chapter": "Chapter 10 – Means of Egress",
        "text": (
            "IBC 2023 §1006 NUMBER OF EXITS AND EXIT ACCESS DOORWAYS. "
            "§1006.3 Egress from stories: Two exits or exit access doorways from any space "
            "shall be provided where the occupant load of the space exceeds the values in "
            "Table 1006.2.1. "
            "§1006.3.3 Single exit: Only one exit shall be required from spaces or stories "
            "if the number of occupants does not exceed 49, the exit is not more than four "
            "stories above grade plane, and the common path of egress travel does not exceed "
            "125 feet for sprinklered buildings or 75 feet for non-sprinklered buildings. "
            "§1006.3.4 Buildings with more than 500 occupants: At least three exits shall "
            "be provided from every story that has an occupant load of 501 to 1,000. "
            "Buildings with occupant loads exceeding 1,000 require at least four exits."
        ),
    },
    {
        "id": "IBC-1010",
        "section": "IBC 2023 §1010",
        "chapter": "Chapter 10 – Means of Egress",
        "text": (
            "IBC 2023 §1010.1 DOORS. Egress doors shall be of the pivoted or side-hinged "
            "swinging type. Exceptions: 1. Private garages, office areas, factory and storage "
            "areas with an occupant load of 10 or less. "
            "§1010.1.9 PANIC AND FIRE EXIT HARDWARE. Doors serving a Group A or E occupancy "
            "with an occupant load of 50 or more and any occupancy with an occupant load of "
            "100 or more shall not be provided with a latch or lock other than panic hardware "
            "or fire exit hardware. "
            "§1010.1.1 Size of doors: The minimum width of each door opening shall be "
            "sufficient for the occupant load thereof but shall not be less than 32 inches "
            "where the door is in the fully open position. The minimum clear opening width "
            "for all doors in the means of egress shall be 32 inches with the door in the "
            "fully open position. For doors serving an occupant load of 50 or more, the "
            "door width shall be not less than 36 inches."
        ),
    },
    # ── Chapter 11: Accessibility ────────────────────────────────────────
    {
        "id": "IBC-1101",
        "section": "IBC 2023 §1101",
        "chapter": "Chapter 11 – Accessibility",
        "text": (
            "IBC 2023 §1101.2 DESIGN. Buildings and facilities shall be designed and constructed "
            "to be accessible in accordance with this code and ICC A117.1. "
            "§1101.1 SCOPE: The provisions of this chapter and ICC A117.1 shall control the "
            "design and construction of facilities for accessibility to persons with physical "
            "disabilities. "
            "§1102.1 DEFINITIONS: ICC A117.1 means the ICC/ANSI A117.1 standard entitled "
            "Accessible and Usable Buildings and Facilities. "
            "§1103 SCOPING REQUIREMENTS: Accessible buildings, facilities, sites and spaces "
            "shall be provided in accordance with Sections 1104 through 1112. "
            "§1104 ACCESSIBLE ROUTE: Not less than one accessible route shall connect "
            "accessible building or facility entrances with all accessible spaces and elements "
            "and facilities within the building or facility."
        ),
    },
    {
        "id": "IBC-1109",
        "section": "IBC 2023 §1109",
        "chapter": "Chapter 11 – Accessibility",
        "text": (
            "IBC 2023 §1109 OTHER FEATURES AND FACILITIES. "
            "§1109.2 TOILET AND BATHING FACILITIES: Where toilet rooms are provided, each "
            "toilet room shall be accessible. Where bathing rooms are provided, each bathing "
            "room shall be accessible. "
            "§1109.7 PARKING SPACES: Where parking is provided, accessible parking spaces "
            "shall be provided in accordance with Table 1109.7. For 1-25 total spaces: "
            "1 accessible space required. For 26-50: 2 spaces. For 51-75: 3 spaces. "
            "For 76-100: 4 spaces. For 101-150: 5 spaces. For 151-200: 6 spaces. "
            "For 201-300: 7 spaces. For 301-400: 8 spaces. "
            "Each lot shall have at least one van-accessible parking space. "
            "Van-accessible spaces shall have an 8-foot wide adjacent access aisle."
        ),
    },
    # ── Chapter 16: Structural Design ───────────────────────────────────
    {
        "id": "IBC-1613",
        "section": "IBC 2023 §1613",
        "chapter": "Chapter 16 – Structural Design",
        "text": (
            "IBC 2023 §1613 EARTHQUAKE LOADS. Every structure, and portion thereof, including "
            "nonstructural components that are permanently attached to structures, and their "
            "supports and attachments, shall be designed and constructed to resist the effects "
            "of earthquake motions in accordance with ASCE 7. "
            "§1613.2.1 SEISMIC DESIGN CATEGORIES: Based on the occupancy category and the "
            "design spectral response acceleration, each structure shall be assigned a seismic "
            "design category (SDC) from A through F. "
            "SDC D, E, or F: Structures require engineered seismic-resistant systems including "
            "moment frames, shear walls, or braced frames. Structural drawings and analysis "
            "are mandatory. "
            "SDC A or B: Minimal seismic requirements. "
            "§1613.3 SITE CLASSIFICATION: Site classes A through F based on shear wave velocity, "
            "standard penetration resistance, or undrained shear strength."
        ),
    },
    {
        "id": "IBC-1604",
        "section": "IBC 2023 §1604",
        "chapter": "Chapter 16 – Structural Design",
        "text": (
            "IBC 2023 §1604 GENERAL STRUCTURAL DESIGN. Buildings, structures and parts thereof "
            "shall be designed and constructed in accordance with strength design, load and "
            "resistance factor design, allowable stress design, empirical design or conventional "
            "construction methods, as permitted by the applicable material chapters. "
            "§1604.3 SERVICEABILITY: Structural systems and members thereof shall be designed "
            "to have adequate stiffness to limit deflections, lateral drift, vibration, or any "
            "other deformations that adversely affect the intended use and performance of "
            "nonstructural components and cladding. "
            "§1604.5 LOAD COMBINATIONS: Structures and portions thereof shall resist the most "
            "critical effects from the combinations of loads as prescribed per ASCE 7."
        ),
    },
    # ── ADA / ICC A117.1 ─────────────────────────────────────────────────
    {
        "id": "ADA-4.3",
        "section": "ADA §4.3 / ICC A117.1",
        "chapter": "ADA Standards for Accessible Design",
        "text": (
            "ADA §4.3 ACCESSIBLE ROUTE. An accessible route shall connect accessible elements "
            "and spaces on a site. A path of travel is accessible if: "
            "Width: At least 36 inches clear. In passing spaces at least 60 inches wide. "
            "Slope: Running slope no greater than 1:20 (5%). Cross slope no greater than 1:48. "
            "Changes in level: Vertical changes up to 1/4 inch permitted. Changes between "
            "1/4 inch and 1/2 inch shall be beveled with a slope no greater than 1:2. "
            "Changes in level greater than 1/2 inch shall be ramped. "
            "ICC A117.1 §403 WALKING SURFACES: Floor or ground surfaces on accessible routes "
            "shall be stable, firm, and slip resistant. "
            "Protruding objects: Objects with leading edges more than 27 inches and not more "
            "than 80 inches above the floor shall protrude no more than 4 inches horizontally."
        ),
    },
    {
        "id": "ADA-4.6",
        "section": "ADA §4.6 / ICC A117.1 §502",
        "chapter": "ADA Standards for Accessible Design",
        "text": (
            "ADA §4.6 PARKING AND PASSENGER LOADING ZONES. "
            "§4.6.2 LOCATION: Accessible parking spaces serving a building shall be located "
            "on the shortest accessible route to an accessible entrance. "
            "§4.6.3 PARKING SPACES: Accessible parking spaces shall be at least 96 inches "
            "wide. Parking access aisles shall be part of an accessible route to the building "
            "entrance. Two accessible parking spaces may share a common access aisle. "
            "§4.6.4 VAN-ACCESSIBLE SPACES: One in every 6 accessible spaces, or fraction "
            "thereof, shall be a van-accessible space with at least 98 inches clear height "
            "and an 8-foot wide access aisle. "
            "ICC A117.1 §502.3: The minimum length of a parking space shall be 18 feet."
        ),
    },
    # ── Energy Code ──────────────────────────────────────────────────────
    {
        "id": "IECC-C401",
        "section": "IECC 2021 §C401",
        "chapter": "IECC 2021 Commercial Energy",
        "text": (
            "IECC 2021 §C401 GENERAL SCOPE. This chapter governs the design and construction "
            "of buildings for energy efficiency. "
            "§C402 BUILDING ENVELOPE: Thermal performance requirements for walls, roofs, "
            "floors, slab-on-grade, and fenestration. Climate zones determine R-value and "
            "U-factor requirements. "
            "§C403 BUILDING MECHANICAL SYSTEMS: Heating, cooling, ventilation, and service "
            "water heating systems shall meet minimum efficiency requirements. "
            "§C404 BUILDING SERVICE WATER HEATING: Water heating systems shall be equipped "
            "with controls that allow shutoff and comply with ASHRAE 90.1. "
            "§C405 BUILDING ELECTRICAL POWER AND LIGHTING: Lighting power density limits "
            "and controls required for all spaces. Interior lighting power allowance per "
            "Table C405.3.2(1) ranges from 0.5 W/sq ft for parking to 1.5 W/sq ft for retail."
        ),
    },
]


# ---------------------------------------------------------------------------
# Vector Store
# ---------------------------------------------------------------------------

@dataclass
class VectorStore:
    """In-memory vector store using numpy cosine similarity. No external deps."""
    chunks:    List[Dict[str, str]] = field(default_factory=list)
    embeddings: Optional[np.ndarray] = None   # shape (N, D)

    def is_ready(self) -> bool:
        return self.embeddings is not None and len(self.embeddings) > 0

    def add(self, chunks: List[Dict[str, str]], vecs: np.ndarray) -> None:
        self.chunks    = chunks
        self.embeddings = vecs.astype(np.float32)
        # L2-normalise for cosine via dot product
        norms = np.linalg.norm(self.embeddings, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1e-9, norms)
        self.embeddings /= norms

    def search(self, query_vec: np.ndarray, top_k: int = 5) -> List[Tuple[float, Dict]]:
        if not self.is_ready():
            return []
        q = query_vec.astype(np.float32)
        q /= max(np.linalg.norm(q), 1e-9)
        scores = self.embeddings @ q
        idx    = np.argsort(scores)[::-1][:top_k]
        return [(float(scores[i]), self.chunks[i]) for i in idx]


# ---------------------------------------------------------------------------
# TF-IDF Fallback (no API key required)
# ---------------------------------------------------------------------------

def _tfidf_score(query: str, text: str) -> float:
    """Simple BM25-inspired keyword overlap score."""
    q_terms = set(re.findall(r'\w+', query.lower()))
    t_terms = re.findall(r'\w+', text.lower())
    t_freq  = {}
    for w in t_terms: t_freq[w] = t_freq.get(w, 0) + 1
    score   = sum(math.log(1 + t_freq.get(w, 0)) for w in q_terms)
    return score / max(math.sqrt(len(t_terms)), 1)


def keyword_search(query: str, top_k: int = 5) -> List[Tuple[float, Dict]]:
    scored = [((_tfidf_score(query, c["text"])), c) for c in IBC_CORPUS]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_k]


# ---------------------------------------------------------------------------
# NeMo Retriever Client
# ---------------------------------------------------------------------------

class NeMoRetriever:
    """
    Embeds queries and chunks with nvidia/nv-embedqa-e5-v5,
    reranks with nvidia/nv-rerankqa-mistral-4b-v3.
    Falls back to keyword search when api_key is empty.
    """

    def __init__(self) -> None:
        self._store     = VectorStore()
        self._indexed   = False
        self._api_key   = ""

    # ── Public ────────────────────────────────────────────────────────────

    def set_api_key(self, key: str) -> None:
        self._api_key = key

    def index_corpus(self) -> None:
        """
        Embed the full IBC corpus.
        Called once at startup when an API key is available.
        Falls back to marking TF-IDF mode if no key.
        """
        if not self._api_key:
            logger.info("No NIM API key — RAG using TF-IDF keyword search")
            self._indexed = True   # tfidf mode
            return
        logger.info("Indexing IBC 2023 corpus (%d chunks) via NeMo Retriever…",
                    len(IBC_CORPUS))
        texts = [c["text"] for c in IBC_CORPUS]
        vecs  = self._embed_batch(texts)
        self._store.add(IBC_CORPUS, vecs)
        self._indexed = True
        logger.info("Corpus indexed: %d vectors (dim=%d)", len(vecs), vecs.shape[1])

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        rerank: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Retrieve the top_k most relevant IBC sections for a query.
        Returns list of {section, chapter, text, score}.
        """
        if not self._indexed:
            self.index_corpus()

        if self._api_key and self._store.is_ready():
            q_vec = self._embed_single(query)
            hits  = self._store.search(q_vec, top_k=top_k * 2)
            if rerank and len(hits) > 1:
                hits = self._rerank(query, hits, top_k=top_k)
            else:
                hits = hits[:top_k]
        else:
            hits = keyword_search(query, top_k=top_k)

        return [
            {
                "id":      c.get("id",""),
                "section": c.get("section",""),
                "chapter": c.get("chapter",""),
                "text":    c.get("text","")[:600],
                "score":   round(score, 4),
                "source":  "nemo-retriever" if self._store.is_ready() else "tfidf",
            }
            for score, c in hits
        ]

    def build_compliance_context(
        self,
        spec_description: str,
        top_k: int = 6,
    ) -> str:
        """
        Return a formatted string of retrieved IBC sections for injection
        into the compliance engine system prompt.
        """
        hits = self.retrieve(spec_description, top_k=top_k)
        lines = ["RELEVANT IBC 2023 SECTIONS (retrieved by NeMo Retriever):", ""]
        for i, h in enumerate(hits, 1):
            lines.append(f"[{i}] {h['section']} – {h['chapter']}")
            lines.append(h['text'][:400])
            lines.append("")
        return "\n".join(lines)

    # ── Embedding ─────────────────────────────────────────────────────────

    def _embed_single(self, text: str) -> np.ndarray:
        vecs = self._embed_batch([text])
        return vecs[0]

    def _embed_batch(self, texts: List[str]) -> np.ndarray:
        """Call NeMo Retriever embedding NIM, return (N, D) float32 array."""
        from triton_client import triton_infer
        BATCH = 32
        all_vecs = []
        for i in range(0, len(texts), BATCH):
            batch = texts[i:i+BATCH]
            payload = {
                "model": EMBED_MODEL,
                "input": batch,
                "input_type": "passage",
                "encoding_format": "float",
                "truncate": "END",
            }
            try:
                resp = triton_infer(NIM_EMBED_ENDPOINT, payload, self._api_key)
                vecs = [d["embedding"] for d in resp["data"]]
                all_vecs.extend(vecs)
            except Exception as exc:
                logger.warning("Embedding batch %d failed: %s — using zeros", i, exc)
                all_vecs.extend([[0.0]*EMBED_DIM for _ in batch])

        return np.array(all_vecs, dtype=np.float32)

    # ── Reranking ─────────────────────────────────────────────────────────

    def _rerank(
        self,
        query: str,
        hits: List[Tuple[float, Dict]],
        top_k: int,
    ) -> List[Tuple[float, Dict]]:
        """Rerank hits using nvidia/nv-rerankqa-mistral-4b-v3."""
        from triton_client import triton_infer
        passages = [{"text": c["text"][:512]} for _, c in hits]
        payload  = {
            "model":    RERANK_MODEL,
            "query":    {"text": query},
            "passages": passages,
        }
        try:
            resp    = triton_infer(NIM_RERANK_ENDPOINT, payload, self._api_key)
            scores  = [(r["logit"], hits[r["index"]][1]) for r in resp["rankings"]]
            scores.sort(key=lambda x: x[0], reverse=True)
            return scores[:top_k]
        except Exception as exc:
            logger.warning("Reranking failed: %s — using embedding scores", exc)
            return hits[:top_k]


# ---------------------------------------------------------------------------
# RAG-Augmented Compliance Check
# ---------------------------------------------------------------------------

class RAGComplianceEngine:
    """
    Wraps the existing rule-based ComplianceEngine and augments each
    validation with NeMo Retriever context injected into the findings.

    The rule engine still fires deterministically — RAG adds cited IBC
    section text to each finding's recommendation for richer output.
    """

    def __init__(self, retriever: NeMoRetriever) -> None:
        from compliance import ComplianceEngine
        self._engine    = ComplianceEngine()
        self._retriever = retriever

    def validate(self, spec) -> Any:
        """
        Run rule-based compliance then enrich findings with RAG citations.
        Returns standard ComplianceReport.
        """
        from models import ComplianceFinding, Severity

        report = self._engine.validate(spec)

        # Build a query from the spec for retrieval
        query = (
            f"{spec.occupancy_group.value} occupancy "
            f"{spec.construction_type.value} construction "
            f"{spec.building_type.value} building "
            f"{spec.primary_code.value} "
            f"{'sprinklered' if spec.sprinklered else 'non-sprinklered'} "
            f"seismic SDC {spec.jurisdiction.seismic_design_category}"
        )

        try:
            hits = self._retriever.retrieve(query, top_k=4, rerank=False)
        except Exception as exc:
            logger.warning("RAG retrieval failed during compliance: %s", exc)
            hits = []

        # Attach top retrieved sections to the report as info findings
        if hits:
            for hit in hits[:2]:
                report.findings.append(
                    ComplianceFinding(
                        rule_id      = f"RAG-{hit['id']}",
                        severity     = Severity.INFO,
                        code_section = hit["section"],
                        description  = (
                            f"NeMo Retriever ({hit['source']}, score={hit['score']:.3f}): "
                            f"{hit['text'][:200]}…"
                        ),
                        recommendation = f"See full text: {hit['section']}",
                    )
                )

        return report


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_retriever_instance: Optional[NeMoRetriever] = None


def get_retriever(api_key: str = "") -> NeMoRetriever:
    global _retriever_instance
    if _retriever_instance is None:
        _retriever_instance = NeMoRetriever()
    if api_key and not _retriever_instance._api_key:
        _retriever_instance.set_api_key(api_key)
        _retriever_instance.index_corpus()
    return _retriever_instance
