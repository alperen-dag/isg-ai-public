from dataclasses import dataclass
from ai_service.ppe_types import Violation

PPE_RULES = {"helmet": "Baret Yok", "vest": "Reflektif Yelek Yok",
             "ear_protection": "Kulak Koruyucu Yok",
             "safety_shoes": "İş Güvenliği Ayakkabısı Yok"}
ZONE_VIOLATION = "Yasak Bolge Ihlali"


@dataclass(frozen=True)
class CameraPPERules:
    required: frozenset[str] = frozenset()
    confidence_threshold: float = .5

    def __post_init__(self):
        if not self.required <= PPE_RULES.keys():
            raise ValueError("Unknown PPE rule")
        if not 0 <= self.confidence_threshold <= 1:
            raise ValueError("Invalid PPE threshold")


def load_camera_rules(connection, camera_id):
    rows = connection.execute(
        "SELECT equipment, confidence_threshold FROM kamera_ppe_kurallari "
        "WHERE kamera_id = ? AND aktif = 1", (camera_id,)).fetchall()
    # Separate configs preserve each rule's threshold.
    return [CameraPPERules(frozenset([equipment]), threshold)
            for equipment, threshold in rows]


def evaluate_ppe(people, detections, config, supported_classes):
    violations = []
    for person in people:
        for equipment in sorted(config.required):
            missing = "no_" + equipment
            if missing not in supported_classes:
                continue
            evidence = [d for d in detections if d.person_id == person.person_id
                        and d.confidence >= config.confidence_threshold]
            negatives = [d for d in evidence if d.class_name == missing]
            if negatives and not any(d.class_name == equipment for d in evidence):
                violations.append(Violation(
                    PPE_RULES[equipment], max(d.confidence for d in negatives),
                    person.person_id, "PPE detector açık eksiklik bulgusu; kişi ID servis oturumu kapsamındadır."))
    return violations


def restricted_zone_violation(confidence, person_id=None):
    return Violation(ZONE_VIOLATION, confidence if confidence is not None and 0 < confidence <= 1 else None, person_id)
