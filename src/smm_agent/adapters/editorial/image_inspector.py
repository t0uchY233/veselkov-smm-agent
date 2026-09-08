"""Image validation through Pillow."""

from io import BytesIO

from PIL import Image, ImageStat, UnidentifiedImageError


class InvalidEditorialAsset(ValueError):
    pass


class PillowImageInspector:
    def dimensions(self, payload: bytes) -> tuple[int, int]:
        try:
            with Image.open(BytesIO(payload)) as image:
                image.verify()
            with Image.open(BytesIO(payload)) as image:
                return image.size
        except (UnidentifiedImageError, OSError) as error:
            raise InvalidEditorialAsset("Asset не является читаемым изображением") from error

    def contrast_score(self, payload: bytes) -> float:
        try:
            with Image.open(BytesIO(payload)) as image:
                return float(ImageStat.Stat(image.convert("L")).stddev[0])
        except (UnidentifiedImageError, OSError) as error:
            raise InvalidEditorialAsset("Asset не является читаемым изображением") from error
