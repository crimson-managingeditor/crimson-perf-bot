import re, html
# ================= SCOOP DEFINITION v6 =================
# v6 is the first version TUNED AGAINST HAND LABELS: 50 News stories the ME
# labeled scoop / not-scoop. Old v5 scored precision 60% / recall 75% on that
# set; v6 scores precision 68% / recall 85% (accuracy 70% -> 78%). Changes,
# each earned from a labeled miss:
#   + SOURCE-BASED markers (quietly / "N people familiar" / "a person familiar"
#     / "confirmed ... to The Crimson") — catch exclusives broken via sources,
#     not documents (e.g. Summers resignation, $10M viewpoint-diversity gifts).
#     On the 50 these fired only on scoops, never on non-scoops.
#   - dropped v5's broad 'crimson-did': its "confirmed that" arm flagged a
#     routine confirmation as a scoop. Kept only the strong "has learned"/
#     "learned that" break phrasing (as 'crimson-learned').
#   ~ callback guard now tolerates an adverb ("a Crimson investigation *later*
#     found") and catches bare "later/previously/earlier found" — so a follow-up
#     crediting a prior Crimson investigation isn't counted as fresh reporting.
# Ceiling note: the residual errors are editorial-judgment calls a regex can't
# resolve — an obtained email used as incidental confirmation of an already-public
# event (reads like a scoop, isn't), and aggregation/source scoops with no
# exclusivity language (reads ordinary, is a scoop). Treat the flag as a strong
# signal, not ground truth.
# A News story is a SCOOP if it presents ORIGINAL reporting the *breaking* story
# would use — evidence obtained/reviewed, a Crimson analysis, source-based
# exclusives — not callbacks that credit a prior scoop, nor explainer/recap formats.

POS = [
 ('obtained',        r'\bobtained by The Crimson\b'),
 ('records-reviewed',r'\b(?:records?|documents?|filings?|e-?mails?|messages?|texts?|memos?|data|datasets?|recordings?|slides?|spreadsheets?|letters?|notices?|photos?|photographs?|images?|videos?|footage|reports?|copies|copy|minutes)\b[^.]{0,60}\b(?:obtained|reviewed) by The Crimson\b'),
 ('internal-doc',    r'\binternal\b[^.]{0,50}\b(?:obtained|reviewed) by The Crimson\b'),
 ('crimson-analysis',r'\b(?:a|an) (?:Crimson (?:analysis|review|investigation|examination)|analysis by The Crimson)\b'),
 ('not-previously',  r'\bnot\b[^.]{0,25}\b(?:previously|publicly)\b[^.]{0,18}\b(?:reported|disclosed|known|revealed|announced|made public)\b'),
 ('crimson-learned', r'\bThe Crimson (?:has learned|learned that)\b'),
 # --- source-based exclusivity (v6) ---
 ('quietly',         r'\bquietly\b'),
 ('srcs-familiar',   r'according to (?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|several|multiple|numerous|\d+)\s+(?:current or former |former |current )?(?:people|persons?|sources?|individuals?|officials?|administrators?|employees?|staff members?|faculty|professors?|students?)\b[^.]{0,45}(?:familiar|briefed|with (?:direct )?knowledge|who (?:were|was|spoke|had|requested|asked)|granted anonymity|on condition)'),
 ('person-familiar', r'\b(?:a|one|another)\s+(?:person|source|individual|official|administrator|employee)\s+(?:familiar with|briefed on|with (?:direct )?knowledge)'),
 ('confirmed-crimson',r'\bconfirmed (?:the [a-z ]+ )?(?:to|in an? [a-z ]+ with) The Crimson\b'),
]
POSC = [(k, re.compile(p, re.I)) for k, p in POS]

# CALLBACK GUARD — a marker hit is a back-reference (crediting an earlier scoop),
# not fresh reporting, if any of these appear within ~90 chars of it.
# v4: added after|since|following|before|later. v6: allow an adverb inside
# "a Crimson investigation <adv> found", and catch bare "later/previously/earlier
# found" (the marker text itself is blanked before this check, so the temporal
# adverb is what remains to signal the callback).
CALLBACK = re.compile(
 r'first reported|previously reported|had reported|The Crimson reported|reported by The Crimson'
 r'|as (?:The|the) Crimson|The Crimson revealed|The Crimson broke|The Crimson found'
 r'|a Crimson (?:investigation|analysis|review|examination)\b[^.]{0,25}\b(?:found|revealed|showed|reported|documented)'
 r'|\b(?:later|previously|earlier|had|also) (?:found|revealed|reported|showed|documented|concluded)\b'
 r'|\b(?:weeks?|days?|months?|hours?)\b (?:ago|earlier|prior|after|since|before|later)\b'
 r'|(?:following|after|since|before) (?:a|an|the|The) (?:Crimson|report|investigation|story|article)'
 r'|last (?:week|month|year|spring|fall|semester|Monday|Tuesday|Wednesday|Thursday|Friday)'
 r'|earlier this (?:week|month|year|semester)', re.I)

# EXPLAINER GUARD — recap/Q&A/guide formats reuse prior reporting; the desk
# encodes the format in the URL slug. These are never the break.
EXPLAINER_SLUG = re.compile(
 r'-(?:explained|explainer|what-to-know|whattoknow|everything-to-know|a-guide|guide-to|faq|q-and-a|qanda|by-the-numbers|timeline|recap)(?:-|/|$)', re.I)

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
