"""Export already accepted pose people without changing zone eligibility."""
import logging
from ai_service.ppe_types import Person


def append_person(people, bbox, confidence, keypoints, index, keypoint_conf):
    try:
        points = tuple((float(xy[0]), float(xy[1]), float(conf))
                       for xy, conf in zip(keypoints.xy[index].tolist(), keypoint_conf))
        people.append(Person(str(len(people)), bbox, confidence, points))
    except (AttributeError, IndexError, TypeError, ValueError):
        logging.getLogger(__name__).warning("Pose export unavailable for PPE", exc_info=True)
