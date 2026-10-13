"""UK SIC 2007 industry codes: names of their divisions (the first two digits).

Companies House files every company under up to four five-digit SIC 2007 codes
("62012 Business and domestic software development"). The workbook shows a code
with its division's name ("62012 (computer programming and consultancy)"); the
divisions are the UK SIC 2007 / NACE Rev. 2 divisions, shortened. Two codes are
Companies House's own: 99999 (dormant company) and 74990 (non-trading company).
"""

from __future__ import annotations

SPECIAL = {"99999": "dormant company", "74990": "non-trading company"}

DIVISIONS = {
    "01": "crop and animal production", "02": "forestry", "03": "fishing and aquaculture",
    "05": "coal mining", "06": "oil and gas extraction", "07": "metal ore mining",
    "08": "other mining and quarrying", "09": "mining support",
    "10": "food manufacturing", "11": "beverages", "12": "tobacco products", "13": "textiles",
    "14": "clothing", "15": "leather goods", "16": "wood products", "17": "paper products",
    "18": "printing", "19": "coke and refined petroleum", "20": "chemicals",
    "21": "pharmaceuticals", "22": "rubber and plastic products",
    "23": "non-metallic mineral products", "24": "basic metals", "25": "metal products",
    "26": "computer and electronic products", "27": "electrical equipment",
    "28": "machinery", "29": "motor vehicles", "30": "other transport equipment",
    "31": "furniture", "32": "other manufacturing", "33": "repair and installation",
    "35": "energy supply", "36": "water supply", "37": "sewerage", "38": "waste management",
    "39": "remediation", "41": "construction of buildings", "42": "civil engineering",
    "43": "specialised construction", "45": "motor trade", "46": "wholesale trade",
    "47": "retail trade", "49": "land transport", "50": "water transport",
    "51": "air transport", "52": "warehousing and logistics", "53": "postal and courier",
    "55": "accommodation", "56": "food and drink service", "58": "publishing",
    "59": "film, TV and music production", "60": "broadcasting", "61": "telecommunications",
    "62": "computer programming and consultancy", "63": "information services",
    "64": "financial services", "65": "insurance and pensions",
    "66": "auxiliary financial services", "68": "real estate", "69": "legal and accounting",
    "70": "head offices and management consultancy", "71": "architecture and engineering",
    "72": "scientific research", "73": "advertising and market research",
    "74": "other professional services", "75": "veterinary", "77": "rental and leasing",
    "78": "employment activities", "79": "travel agencies and tour operators",
    "80": "security and investigation", "81": "building services and landscaping",
    "82": "office administration and business support", "84": "public administration",
    "85": "education", "86": "human health", "87": "residential care", "88": "social work",
    "90": "creative arts and entertainment", "91": "libraries, museums and culture",
    "92": "gambling", "93": "sports and recreation", "94": "membership organisations",
    "95": "repair of computers and household goods", "96": "other personal services",
    "97": "households as employers", "98": "households' own production",
    "99": "extraterritorial organisations",
}


def describe(code: str) -> str:
    """"62012" -> "62012 (computer programming and consultancy)"; unknown codes as given."""
    code = str(code or "").strip()
    name = SPECIAL.get(code) or DIVISIONS.get(code[:2])
    return f"{code} ({name})" if name else code


def division(code: str) -> str:
    """The division's name ("computer programming and consultancy"), or ""."""
    code = str(code or "").strip()
    return SPECIAL.get(code) or DIVISIONS.get(code[:2], "")
