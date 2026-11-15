"""Bounded subprocess: parses untrusted files without Django/database credentials in the environment."""

import json
import shutil
import sys
import warnings
from pathlib import Path


def limits():
    if sys.platform != "linux":
        return
    try:
        import resource
    except ImportError:  # Other platforms: wall-time and input/output/pixel/page limits still apply.
        return
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_FSIZE, (25 * 1024 * 1024, 25 * 1024 * 1024))


def process(source, output, kind, suffix):
    from PIL import Image, ImageOps
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

    Image.MAX_IMAGE_PIXELS = 20_000_000
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    header = Path(source).read_bytes()[:16]
    if header.startswith(b"%PDF-"):
        if kind != "document" or suffix != ".pdf":
            raise ValueError()
        reader = PdfReader(source, strict=True)
        if reader.is_encrypted:
            raise ValueError()
        pages = len(reader.pages)
        if not 1 <= pages <= 100:
            raise ValueError()
        # Reject active actions, attachments and interactive forms. Do not rewrite signed bytes.
        forbidden = {
            "/JS",
            "/JavaScript",
            "/OpenAction",
            "/AA",
            "/Launch",
            "/EmbeddedFiles",
            "/EF",
            "/RichMedia",
            "/XFA",
            "/AcroForm",
        }
        queue = [reader.trailer]
        seen = set()
        count = 0
        while queue:
            obj = queue.pop()
            count += 1
            if count > 20000:
                raise ValueError()
            if isinstance(obj, IndirectObject):
                key = (obj.idnum, obj.generation)
                if key in seen:
                    continue
                seen.add(key)
                queue.append(obj.get_object())
            elif isinstance(obj, DictionaryObject):
                if forbidden.intersection(obj.keys()):
                    raise ValueError()
                queue.extend(obj.values())
            elif isinstance(obj, ArrayObject):
                queue.extend(obj)
        shutil.copyfile(source, output)
        return {
            "mime_type": "application/pdf",
            "extension": "pdf",
            "page_count": pages,
            "width": None,
            "height": None,
        }
    with Image.open(source) as image:
        image_format = image.format
        expected = {"JPEG": {".jpg", ".jpeg"}, "PNG": {".png"}}
        if image_format not in expected or suffix not in expected[image_format]:
            raise ValueError()
        if image.width * image.height > 20_000_000 or getattr(image, "n_frames", 1) != 1:
            raise ValueError()
        image.verify()
    with Image.open(source) as image:
        image.load()
        oriented = ImageOps.exif_transpose(image)
        mode = "RGB" if image_format == "JPEG" else "RGBA"
        # Fresh pixel-only image strips EXIF/GPS, PNG text chunks and embedded profiles.
        converted = oriented.convert(mode)
        clean = Image.new(mode, converted.size)
        clean.paste(converted)
        clean.save(output, format=image_format, **({"quality": 95} if image_format == "JPEG" else {}))
        return {
            "mime_type": "image/jpeg" if image_format == "JPEG" else "image/png",
            "extension": "jpg" if image_format == "JPEG" else "png",
            "page_count": None,
            "width": clean.width,
            "height": clean.height,
        }


if __name__ == "__main__":
    try:
        limits()
        result = process(*sys.argv[1:])
        print(json.dumps(result))
    except Exception:
        # No filenames, PDF content or parser exception text leave this worker.
        print('{"error": "invalid_file"}')
        sys.exit(1)
