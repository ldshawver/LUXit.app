"""Flask extensions initialization."""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from extensions import db, csrf


limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)
