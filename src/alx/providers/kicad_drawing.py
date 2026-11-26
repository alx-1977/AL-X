"""Cropped feedback from KiCad's own before/after drawing of each changed file."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageChops

from alx.providers.look_image import keep_image


def crop(before, after):
    """Locate the change in rendered coordinates, including both ends of a move."""
    picture = after if after is not None else before
    if before is None or after is None or before.size != after.size:
        comparison = Image.new('RGB', picture.size, 'white')
    else:
        comparison = before
    box = ImageChops.difference(picture, comparison).getbbox()
    if box:
        x, y, right, bottom = box
        margin = max(30, round(max(right-x, bottom-y) * .08))
        picture = picture.crop((max(0, x-margin), max(0, y-margin),
                                min(picture.width, right+margin), min(picture.height, bottom+margin)))
    picture.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    data = BytesIO()
    picture.save(data, format='PNG')
    return data.getvalue()


def drawings(provider, snapshot, project, changed, copper_changed):
    renderer = provider.engine_paths.get('pdftoppm')
    out = []
    for name in changed:
        schematic = name.endswith('.kicad_sch')
        if not schematic and not name.endswith('.kicad_pcb'):
            continue
        item = {'file': name}
        out.append(item)
        if not renderer or not renderer.is_file() or provider.execute_page is None:
            item['unavailable'] = 'pdftoppm unavailable'
            continue
        try:
            with TemporaryDirectory(dir=snapshot.parent) as directory:
                scratch = Path(directory)
                def raster(folder):
                    source = folder / name
                    if not source.is_file():
                        return None
                    pdf = scratch / 'drawing.pdf'
                    if schematic:
                        # Render a child directly so page one is the requested sheet.
                        provider._export_pdf('schematic', source, pdf,
                                             options=('--exclude-drawing-sheet', '--no-background-color'))
                    else:
                        provider._export_pdf('board' if copper_changed else 'placement', source, pdf)
                    result = provider.execute_page(renderer, ['-f', 1, '-l', 1, '-scale-to', 3200,
                                                   '-png', '-singlefile', pdf.name, 'page'], scratch)
                    if result.returncode:
                        raise ValueError(result.stderr or 'pdftoppm failed')
                    with Image.open(scratch / 'page.png') as image:
                        return image.convert('RGB')

                before, after = raster(snapshot), raster(project)
                item['view'] = 'after' if after is not None else 'removed'
                item['image_sha256'] = keep_image(provider.image_store, crop(before, after))
        except Exception as error:
            # Rendering is optional feedback. A CLI refusal is evidence for the agent.
            item['unavailable'] = str(error)[:300]
    return out
