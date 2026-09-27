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
