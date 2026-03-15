from .generate_elevations import run as generate_elevations
from .get_details import run as get_details

FLOWS = {
    "generate-elevations": generate_elevations,
    "get-details": get_details,
}
