from pathlib import Path

import cloudinary.uploader
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError

from rental.models import Suit


class Command(BaseCommand):
    help = "Copy local suit catalog images to the configured durable media storage."

    def handle(self, *args, **options):
        if not settings.USE_CLOUDINARY:
            raise CommandError(
                "Configure all Cloudinary environment variables before syncing suit images."
            )

        media_root = Path(settings.MEDIA_ROOT)
        suit_images = media_root / "suits"
        if not suit_images.is_dir():
            raise CommandError(f"Suit image directory does not exist: {suit_images}")

        uploaded = 0
        skipped = 0
        for source_path in sorted(suit_images.rglob("*")):
            if not source_path.is_file():
                continue

            source_name = source_path.relative_to(media_root).as_posix()
            public_id = source_path.relative_to(media_root).with_suffix("").as_posix()
            if not default_storage.exists(public_id):
                cloudinary.uploader.upload(
                    str(source_path),
                    public_id=public_id,
                    resource_type="image",
                    overwrite=False,
                )
                uploaded += 1
                self.stdout.write(f"Uploaded {source_name}")
            else:
                skipped += 1
            Suit.objects.filter(image=source_name).update(image=public_id)

        self.stdout.write(
            self.style.SUCCESS(
                f"Suit images synced: {uploaded} uploaded, {skipped} already present."
            )
        )
