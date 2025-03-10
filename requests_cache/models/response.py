from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import TYPE_CHECKING, List, Optional, Tuple, Union

import attr
from attr import define, field
from requests import PreparedRequest, Response
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict
from urllib3._collections import HTTPHeaderDict

from ..cache_control import ExpirationTime, get_expiration_datetime
from . import CachedHTTPResponse, CachedRequest

DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S %Z'  # Format used for __str__ only
HeaderList = List[Tuple[str, str]]
logger = getLogger(__name__)


@define(auto_attribs=False, slots=False)
class CachedResponse(Response):
    """A class that emulates :py:class:`requests.Response`, with some additional optimizations
    for serialization.
    """

    _content: bytes = field(default=None)
    _next: Optional[CachedRequest] = field(default=None)
    cache_key: Optional[str] = None  # Not serialized; set by BaseCache.get_response()
    cookies: RequestsCookieJar = field(factory=RequestsCookieJar)
    created_at: datetime = field(factory=datetime.utcnow)
    elapsed: timedelta = field(factory=timedelta)
    encoding: str = field(default=None)
    expires: Optional[datetime] = field(default=None)
    headers: CaseInsensitiveDict = field(factory=CaseInsensitiveDict)
    history: List['CachedResponse'] = field(factory=list)  # type: ignore
    raw: CachedHTTPResponse = field(factory=CachedHTTPResponse, repr=False)
    reason: str = field(default=None)
    request: CachedRequest = field(factory=CachedRequest)  # type: ignore
    status_code: int = field(default=0)
    url: str = field(default=None)

    def __attrs_post_init__(self):
        """Re-initialize raw response body after deserialization"""
        if self.raw._body is None and self._content is not None:
            self.raw.reset(self._content)
        if not self.raw.headers:
            self.raw.headers = HTTPHeaderDict(self.headers)

    @classmethod
    def from_response(
        cls,
        original_response: Union[Response, 'CachedResponse'],
        expires: datetime = None,
        **kwargs,
    ):
        """Create a CachedResponse based on an original Response or another CachedResponse object"""
        if isinstance(original_response, CachedResponse):
            return attr.evolve(original_response, expires=expires)
        obj = cls(expires=expires, **kwargs)

        # Copy basic attributes
        for k in Response.__attrs__:
            setattr(obj, k, getattr(original_response, k, None))

        # Store request, raw response, and next response (if it's a redirect response)
        obj.request = CachedRequest.from_request(original_response.request)
        obj.raw = CachedHTTPResponse.from_response(original_response)
        obj._next = (
            CachedRequest.from_request(original_response.next) if original_response.next else None
        )

        # Store response body, which will have been read & decoded by requests.Response by now
        obj._content = original_response.content

        # Copy redirect history, if any; avoid recursion by not copying redirects of redirects
        obj.history = []
        if not obj.is_redirect:
            for redirect in original_response.history:
                obj.history.append(cls.from_response(redirect))

        return obj

    @property
    def _content_consumed(self) -> bool:
        """For compatibility with requests.Response; will always be True for a cached response"""
        return True

    @_content_consumed.setter
    def _content_consumed(self, value: bool):
        pass

    @property
    def from_cache(self) -> bool:
        return True

    @property
    def is_expired(self) -> bool:
        """Determine if this cached response is expired"""
        return self.expires is not None and datetime.utcnow() >= self.expires

    @property
    def ttl(self) -> Optional[int]:
        """Get time to expiration in seconds"""
        if self.expires is None or self.is_expired:
            return None
        delta = self.expires - datetime.utcnow()
        return int(delta.total_seconds())

    @property
    def next(self) -> Optional[PreparedRequest]:
        """Returns a PreparedRequest for the next request in a redirect chain, if there is one."""
        return self._next.prepare() if self._next else None

    def reset_expiration(self, expire_after: ExpirationTime) -> bool:
        """Set a new expiration for this response, and determine if it is now expired"""
        self.expires = get_expiration_datetime(expire_after)
        return self.is_expired

    @property
    def size(self) -> int:
        """Get the size of the response body in bytes"""
        return len(self.content) if self.content else 0

    def __getstate__(self):
        """Override pickling behavior from ``requests.Response.__getstate__``"""
        return self.__dict__

    def __setstate__(self, state):
        """Override pickling behavior from ``requests.Response.__setstate__``"""
        for name, value in state.items():
            setattr(self, name, value)

    def __str__(self):
        return (
            f'request: {self.request}, response: {self.status_code} '
            f'({format_file_size(self.size)}), created: {format_datetime(self.created_at)}, '
            f'expires: {format_datetime(self.expires)} ({"stale" if self.is_expired else "fresh"})'
        )


def format_datetime(value: Optional[datetime]) -> str:
    """Get a formatted datetime string in the local time zone"""
    if not value:
        return "N/A"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime(DATETIME_FORMAT)


def format_file_size(n_bytes: int) -> str:
    """Convert a file size in bytes into a human-readable format"""
    filesize = float(n_bytes or 0)

    def _format(unit):
        return f'{int(filesize)} {unit}' if unit == 'bytes' else f'{filesize:.2f} {unit}'

    for unit in ['bytes', 'KiB', 'MiB', 'GiB']:
        if filesize < 1024 or unit == 'GiB':
            return _format(unit)
        filesize /= 1024

    if TYPE_CHECKING:
        return _format(unit)


def set_response_defaults(
    response: Union[Response, CachedResponse], cache_key: str = None
) -> Union[Response, CachedResponse]:
    """Set some default CachedResponse values on a requests.Response object, so they can be
    expected to always be present
    """
    if not isinstance(response, CachedResponse):
        response.cache_key = cache_key  # type: ignore
        response.created_at = None  # type: ignore
        response.expires = None  # type: ignore
        response.from_cache = False  # type: ignore
        response.is_expired = False  # type: ignore
    return response
