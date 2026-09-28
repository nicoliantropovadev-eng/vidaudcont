"""Medical-topic hint from transcript excerpts: share of medical vocabulary per 100 words."""
import re
from collections import Counter

MED_VOCAB = """
patient doctor nurse clinic clinical clinician hospital ward gp nhs consultant surgery surgeon physician paramedic midwife
pharmacist pharmacy practitioner registrar appointment referral refer admission admitted discharge outpatient inpatient
symptom pain painful ache aching sore soreness swelling swollen tender tenderness lump bleeding bruise bruising itch itchy
nausea vomiting dizzy dizziness fatigue tired tiredness breathless breathlessness cough wheeze fever temperature
examination examine exam inspect inspection palpate palpation percussion auscultate auscultation stethoscope
diagnosis diagnose diagnosed prognosis treatment treat therapy therapist therapeutic medication medicine tablet pill
dose dosage drug prescription prescribe prescribed antibiotic antibiotics injection inhaler vaccine vaccination
side effect effects operation procedure anaesthetic anesthetic biopsy scan ultrasound xray mri ct ecg blood bloods test
result level range sample urine stool smear swab catheter cannula
pressure pulse heart cardiac cardiovascular chest lung respiratory abdomen abdominal stomach bowel liver kidney bladder
pelvis pelvic uterus cervix cervical prostate breast thyroid gland lymph node spleen pancreas oesophagus esophagus
nerve neurological cranial reflex muscle joint bone fracture spine spinal skull brain skin rash wound ulcer
eye vision pupil retina sclera conjunctiva eyelid ear hearing nose sinus throat tonsil neck mouth tongue teeth
limb leg arm hand foot knee hip shoulder elbow wrist ankle finger toe
infection infected virus viral bacterial inflammation cancer tumour tumor malignant benign diabetes diabetic insulin
asthma allergy allergic anaemia anemia haemoglobin hemoglobin iron deficiency cholesterol stroke seizure epilepsy
dementia arthritis hypertension obesity pneumonia sepsis murmur cyanosis congenital syndrome chronic acute emergency
airway oxygen saturation pregnancy pregnant labour contraception period menstrual baby newborn child infant paediatric
pediatric immunisation immunization development growth
history presenting complaint concern onset duration severity family social medical past
anxiety anxious depression depressed disorder mental psychiatric psychiatrist psychologist psychology psychotherapy
counselling counseling cbt trauma ptsd bipolar schizophrenia psychosis anorexia bulimia eating addiction alcohol
withdrawal self harm suicidal session client wellbeing panic phobia obsessive compulsive intake assessment
consent confidentiality diet healthy exercise weight smoking lifestyle condition illness disease health healthcare care
""".split()
MED_SET = set(MED_VOCAB)


def _hit(w):
    if w in MED_SET:
        return w
    for suf in ("s", "es", "ies"):
        if w.endswith(suf):
            base = w[: -len(suf)] + ("y" if suf == "ies" else "")
            if base in MED_SET:
                return base
    return None


def topic_score(text, title="", threshold=1.0):
    words = re.findall(r"[a-z]+", (text or "").lower())
    hits = [h for h in (_hit(w) for w in words) if h]
    per100 = 100.0 * len(hits) / max(len(words), 1)
    title_hits = [h for h in (_hit(w) for w in re.findall(r"[a-z]+", (title or "").lower())) if h]
    return {"words": len(words), "hits": len(hits), "per100": round(per100, 1),
            "top_terms": [w for w, _ in Counter(hits).most_common(8)], "title_terms": title_hits,
            "medical": per100 >= threshold or (per100 >= threshold / 2 and bool(title_hits))}


# A clinician speaking to a patient who is there: questions and instructions addressed to "you", replies.
# An examination where the patient mostly listens is still an encounter; a lecture or a voice-over is not.
ENCOUNTER = [r"\b(?:can|could|would|will) you\b", r"\b(?:do|did|have|are|were) you\b", r"\bif you (?:can|could|just)\b",
             r"\bi'?d like (?:you|to)\b", r"\bfor me\b", r"\bi'?m (?:just )?going to\b", r"\blet me (?:know|just|have)\b",
             r"\bis (?:it|that) (?:ok|okay|alright|all right)\b", r"\bdoes (?:it|that|this) (?:hurt|feel)\b",
             r"\bany (?:pain|discomfort|problems?|questions?)\b", r"\bhow (?:are you|do you feel|have you been)\b",
             r"\b(?:deep )?breaths?\b", r"\bbreathe\b", r"\bopen your\b", r"\blook (?:at|up|down|ahead|straight)\b",
             r"\bfollow my\b", r"\bsqueeze\b", r"\brelax\b", r"\b(?:sit|lie|lean) (?:up|down|back|forward|flat)\b",
             r"\bthank you\b", r"\bwell done\b", r"\b(?:very )?good job\b", r"\bnice to meet you\b", r"\bmy name is\b",
             r"\bwhat brings you\b", r"\bhow can i help\b", r"\btell me\b", r"\bi feel\b", r"\bi'?ve been\b",
             r"\bi (?:don'?t|do|did|was|have|had) \w+", r"\byes,? (?:doctor|please|of course)\b"]
LECTURE = [r"\bin this (?:video|lecture|presentation|tutorial|module|session we)\b", r"\bwelcome (?:to|back)\b",
           r"\b(?:students?|examiners?|candidates?|viewers?)\b", r"\bsubscribe\b", r"\bslides?\b",
           r"\btoday we(?:'re| are| will| 'll)\b", r"\bwe(?:'ll| will) (?:discuss|talk|cover|look)\b", r"\bin summary\b",
           r"\bkey points?\b", r"\blearning (?:objectives?|outcomes?)\b"]
_ENC = re.compile("|".join(ENCOUNTER), re.I)
_LEC = re.compile("|".join(LECTURE), re.I)


def encounter_score(text):
    """Phrases of a clinician-patient encounter and of a lecture, per 100 words of the transcript."""
    text = re.sub(r"\[\d+:\d+\]", " ", text or "")
    words = max(len(text.split()), 1)
    return {"words": words, "encounter": round(100 * len(_ENC.findall(text)) / words, 1),
            "lecture": round(100 * len(_LEC.findall(text)) / words, 1)}
