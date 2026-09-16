"""Conservative spatial association, not a PPE classifier or tracker."""
from dataclasses import replace

REGIONS = {"helmet": "head", "ear_protection": "head",
           "vest": "torso", "safety_shoes": "feet"}


def body_region(person, region):
    x1, y1, x2, y2 = person.bbox
    w, h = x2 - x1, y2 - y1
    indices = {"head": (0, 1, 2, 3, 4), "torso": (5, 6, 11, 12),
               "feet": (15, 16)}[region]
    points = [person.keypoints[i] for i in indices
              if i < len(person.keypoints) and person.keypoints[i][2] >= .3
              and x1 <= person.keypoints[i][0] <= x2
              and y1 <= person.keypoints[i][1] <= y2]
    if points:
        px, py = (.18 * w, .12 * h)
        return (max(x1, min(p[0] for p in points) - px),
                max(y1, min(p[1] for p in points) - py),
                min(x2, max(p[0] for p in points) + px),
                min(y2, max(p[1] for p in points) + py))
    lo, hi = {"head": (0, .3), "torso": (.2, .75), "feet": (.75, 1)}[region]
    return x1, y1 + lo * h, x2, y1 + hi * h


def associate(people, detections, threshold):
    associated = []
    for detection in detections:
        equipment = detection.class_name.removeprefix("no_")
        if equipment not in REGIONS or detection.confidence < threshold:
            continue
        x1, y1, x2, y2 = detection.bbox
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        candidates = []
        for person in people:
            rx1, ry1, rx2, ry2 = body_region(person, REGIONS[equipment])
            if rx1 <= cx <= rx2 and ry1 <= cy <= ry2:
                candidates.append(person.person_id)
        # Even a supplied ID cannot resolve geometrically ambiguous evidence.
        if len(candidates) == 1 and detection.person_id in (None, candidates[0]):
            associated.append(replace(detection, person_id=candidates[0]))
    return associated
