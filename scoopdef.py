import re, html
# ================= SCOOP DEFINITION v5 =================
# Stress-tested. v3: dropped "first reported by The Crimson" (it's a callback,
# not a break) + callback guard. v4: added after/since/following to callback
# guard (catches "days after a Crimson investigation") + explainer-slug guard
# (recap/Q&A/"-explained" URLs reuse prior reporting). v5: fixed two stress-test
# bugs — word-boundary so "after" doesn't match inside "Monday afternoon", and
# plural-optional/expanded noun list so singular "email reviewed by The Crimson"
# and photos/notice/copy/video/minutes are caught.
# A News story is a SCOOP if it presents ORIGINAL evidence-based reporting,
# judged by markers the *breaking* story uses — not callbacks a follow-up uses
# to credit a prior scoop, and not explainer/recap formats.

POS = [
 ('obtained',        r'\bobtained by The Crimson\b'),
 ('records-reviewed',r'\b(?:records?|documents?|filings?|e-?mails?|messages?|texts?|memos?|data|datasets?|recordings?|slides?|spreadsheets?|letters?|notices?|photos?|photographs?|images?|videos?|footage|reports?|copies|copy|minutes)\b[^.]{0,60}\b(?:obtained|reviewed) by The Crimson\b'),
 ('internal-doc',    r'\binternal\b[^.]{0,50}\b(?:obtained|reviewed) by The Crimson\b'),
 ('crimson-analysis',r'\b(?:a|an) (?:Crimson (?:analysis|review|investigation|examination)|analysis by The Crimson)\b'),
 ('crimson-did',     r'\bThe Crimson (?:conducted|compiled|analyzed|reviewed|surveyed|mapped|identified|calculated|found that|has learned|learned that|has confirmed|confirmed that)\b'),
 ('not-previously',  r'\bnot\b[^.]{0,25}\b(?:previously|publicly)\b[^.]{0,18}\b(?:reported|disclosed|known|revealed|announced|made public)\b'),
]
POSC = [(k, re.compile(p, re.I)) for k, p in POS]

# CALLBACK GUARD — a marker hit is a back-reference (crediting an earlier scoop),
# not fresh reporting, if any of these appear within ~90 chars of it.
# v4: added after|since|following|before|later so "days after a Crimson
# investigation found..." is recognized as a callback.
CALLBACK = re.compile(
 r'first reported|previously reported|had reported|The Crimson reported|reported by The Crimson'
 r'|as (?:The|the) Crimson|The Crimson revealed|The Crimson broke|The Crimson found'
 r'|a Crimson (?:investigation|analysis|review|examination) (?:found|revealed|showed|reported|documented)'
 r'|\b(?:weeks?|days?|months?|hours?)\b (?:ago|earlier|prior|after|since|before|later)\b'
 r'|(?:following|after|since|before) (?:a|an|the|The) (?:Crimson|report|investigation|story|article)'
 r'|last (?:week|month|year|spring|fall|semester|Monday|Tuesday|Wednesday|Thursday|Friday)'
 r'|earlier this (?:week|month|year|semester)', re.I)

# EXPLAINER GUARD — recap/Q&A/guide formats reuse prior reporting; the desk
# encodes the format in the URL slug. These are never the break.
EXPLAINER_SLUG = re.compile(
 r'-(?:explained|explainer|what-to-know|whattoknow|everything-to-know|a-guide|guide-to|faq|q-and-a|qanda|by-the-numbers|timeline|recap|explainer)(?:-|/|$)', re.I)

def clean(t):
    t=re.sub(r'\{[^{}]*\}',' ',t or ''); t=re.sub(r'<[^>]+>',' ',t)
    return re.sub(r'\s+',' ',html.unescape(t)).strip()

def scoop_markers(text, url=None):
    """marker keys surviving the callback + explainer guards; [] = not a scoop."""
    if url and EXPLAINER_SLUG.search(url):
        return []
    t = clean(text)
    hits=[]
    for k, rx in POSC:
        for m in rx.finditer(t):
            i, j = m.start(), m.end()
            window = t[max(0,i-90):min(len(t),j+90)]
            probe = window[:90] + window[90+(j-i):]   # blank out the marker itself
            if not CALLBACK.search(probe):
                hits.append(k); break
    return hits

def is_scoop(text, url=None): return bool(scoop_markers(text, url))
