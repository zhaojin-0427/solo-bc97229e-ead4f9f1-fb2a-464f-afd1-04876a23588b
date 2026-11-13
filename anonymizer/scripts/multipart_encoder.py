"""Tiny multipart/form-data encoder used by the offline web test."""
import uuid


def encode(fields, files):
    boundary = "----anonboundary" + uuid.uuid4().hex
    lines = []
    for name, value in fields.items():
        lines.append(f"--{boundary}".encode())
        lines.append(
            f'Content-Disposition: form-data; name="{name}"'.encode()
        )
        lines.append(b"")
        lines.append(value.encode("utf-8") if isinstance(value, str) else value)
    for name, (filename, data) in files.items():
        lines.append(f"--{boundary}".encode())
        lines.append(
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"'.encode()
        )
        lines.append(b"Content-Type: application/octet-stream")
        lines.append(b"")
        lines.append(data)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")
    body = b"\r\n".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"
