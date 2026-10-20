"""Bounded logical-line slices; hashes are supplied by the owning file adapter."""
import re


MAX_LINE_NUMBER = 2**53 - 1
MAX_RANGE_LINES = 200
MAX_RANGE_CHARS = 12000


def line_range(text, start_line, end_line):
    if (type(start_line) is not int or type(end_line) is not int
            or not 1 <= start_line <= end_line <= MAX_LINE_NUMBER):
        raise ValueError('Expected positive integer line numbers with start_line <= end_line.')
    if end_line - start_line + 1 > MAX_RANGE_LINES:
        raise ValueError('Range exceeds 200 lines; request a narrower range.')
    # CRLF is one separator. Keep original line endings and omit a phantom final line.
    lines = [line for line in re.findall(r'[^\r\n]*(?:\r\n|\r|\n|$)', text) if line]
    if start_line > len(lines) and (lines or start_line != 1):
        raise ValueError('start_line is beyond the end of the file.')
    content = ''.join(lines[start_line - 1:end_line])
    if len(content) > MAX_RANGE_CHARS:
        raise ValueError('Range exceeds 12000 characters; request a narrower range.')
    return {'start_line': start_line if lines else 0, 'end_line': min(end_line, len(lines)),
            'total_lines': len(lines), 'content': content}
