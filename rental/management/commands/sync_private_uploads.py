from pathlib import Path

import cloudinary.uploader
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from rental.models import Profile, SuitRequest
from rental.storage import private_media_storage


class Command(BaseCommand):
    help = "Copy customer and identity uploads to authenticated Cloudinary storage."

    def handle(self, *args, **options):
        if not settings.USE_CLOUDINARY:
            raise CommandError(
                "Configure all Cloudinary environment variables before syncing private uploads."
            )

        media_root = Path(settings.MEDIA_ROOT)
        uploaded = 0
        skipped = 0
        for directory in ("customers", "profiles", "ids"):
            source_root = media_root / directory
            if not source_root.is_dir():
                continue

            for source_path in sorted(source_root.rglob("*")):
                if not source_path.is_file():
                    continue

                source_name = source_path.relative_to(media_root).as_posix()
                public_id = source_path.relative_to(media_root).with_suffix("").as_posix()
                if not private_media_storage.exists(public_id):
                    cloudinary.uploader.upload(
                        str(source_path),
                        public_id=public_id,
                        resource_type="image",
                        type="authenticated",
                        overwrite=False,
                    )
                    uploaded += 1
                    self.stdout.write(f"Uploaded private file {source_name}")
                else:
                    skipped += 1
                if directory == "customers":
                    SuitRequest.objects.filter(customer_image=source_name).update(
                        customer_image=public_id
                    )
                elif directory == "profiles":
                    Profile.objects.filter(profile_photo=source_name).update(
                        profile_photo=public_id
                    )
                elif directory == "ids":
                    Profile.objects.filter(id_document=source_name).update(
                        id_document=public_id
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f"Private uploads synced: {uploaded} uploaded, {skipped} already present."
            )
        )
