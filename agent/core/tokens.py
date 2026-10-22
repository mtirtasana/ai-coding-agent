"""Token estimates are request guards, never provider-reported accounting."""
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TokenEstimate:
    tokens: int
    method: str


def request_text(messages, tools):
    # Include structure, names, IDs and schemas omitted by the character metric.
    return json.dumps({'messages': [asdict(m) for m in messages],
                       'tools': [asdict(t) for t in tools]}, ensure_ascii=False,
                      separators=(',', ':'))


class ByteTokenEstimator:
    def estimate(self, messages, tools):
        # A deliberately conservative fallback for byte-based tokenizers.
        # Providers can add hidden framing; this is not a billing guarantee.
        return TokenEstimate(len(request_text(messages, tools).encode('utf-8')) +
                             32 * (len(messages) + len(tools)) + 256, 'utf8-bytes+margin')
