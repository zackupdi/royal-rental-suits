import cloudinary
import cloudinary.uploader
from django.conf import settings
from django.core.files.storage import FileSystemStorage, Storage
from django.utils.deconstruct import deconstructible

from cloudinary import CloudinaryResource
from cloudinary_storage.storage import MediaCloudinaryStorage


class PrivateCloudinaryStorage(MediaCloudinaryStorage):
    def _upload(self, name, content):
        options = {
            "use_filename": True,
            "resource_type": self._get_resource_type(name),
            "type": "authenticated",
            "tags": self.TAG,
        }
        folder = name.rsplit("/", 1)[0] if "/" in name else ""
        if folder:
            options["folder"] = folder
        return cloudinary.uploader.upload(content, **options)

    def _get_url(self, name):
        resource = CloudinaryResource(
            self._prepend_prefix(name),
            type="authenticated",
            default_resource_type=self._get_resource_type(name),
            url_options={"sign_url": True, "secure": True},
        )
        return resource.url

    def delete(self, name):
        response = cloudinary.uploader.destroy(
            name,
            invalidate=True,
            resource_type=self._get_resource_type(name),
            type="authenticated",
        )
        return response["result"] == "ok"


@deconstructible
class PrivateMediaStorage(Storage):
    def _storage(self):
        if settings.USE_CLOUDINARY:
            return PrivateCloudinaryStorage()
        return FileSystemStorage(location=settings.MEDIA_ROOT, base_url=settings.MEDIA_URL)

    def _open(self, name, mode="rb"):
        return self._storage().open(name, mode)

    def _save(self, name, content):
        return self._storage().save(name, content)

    def delete(self, name):
        return self._storage().delete(name)

    def exists(self, name):
        return self._storage().exists(name)

    def url(self, name):
        return self._storage().url(name)

    def size(self, name):
        return self._storage().size(name)


private_media_storage = PrivateMediaStorage()
