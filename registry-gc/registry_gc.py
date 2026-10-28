import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TAG_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ForgejoPackageClient:
    def __init__(self, server, owner, name, token):
        self.server = server.rstrip("/")
        self.name = name
        self.token = token
        self.collection = f"/api/v1/packages/{urllib.parse.quote(owner, safe='')}"
        self.package = (
            f"{self.collection}/container/{urllib.parse.quote(name, safe='')}"
        )
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path):
        request = urllib.request.Request(
            self.server + path,
            method=method,
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.status == 204:
                    return None
                return json.load(response)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise

    def versions(self):
        versions = {}
        seen_ids = set()
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {"type": "container", "q": self.name, "limit": 50, "page": page}
            )
            batch = self.request("GET", f"{self.collection}?{query}")
            if not isinstance(batch, list):
                raise TypeError("Invalid package list response")
            if not batch:
                return versions
            for version in batch:
                if not isinstance(version, dict):
                    raise TypeError("Invalid package record")
                identifier = version.get("id")
                if (
                    type(identifier) is not int
                    or identifier <= 0
                    or identifier in seen_ids
                ):
                    raise ValueError("Invalid or repeated package version ID")
                seen_ids.add(identifier)
                if (
                    version.get("type") != "container"
                    or version.get("name") != self.name
                ):
                    continue
                tag = version.get("version")
                # Digest-only versions include architecture manifests referenced by indexes.
                if not isinstance(tag, str) or not TAG_PATTERN.fullmatch(tag):
                    continue
                if tag in versions:
                    raise ValueError("Package tag changed during pagination")
                versions[tag] = version
            page += 1

    def version_path(self, tag):
        return f"{self.package}/{urllib.parse.quote(tag, safe='')}"

    def get(self, tag):
        return self.request("GET", self.version_path(tag))

    def delete(self, tag):
        self.request("DELETE", self.version_path(tag))


def parse_created_at(value):
    if not isinstance(value, str):
        return None
    try:
        created = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if created.tzinfo is None:
        return None
    return created.astimezone(dt.UTC)


def cleanup(client, keep_tag, now, *, retention_days, content_tags_only=False):
    if retention_days is None and not content_tags_only:
        raise ValueError("Disabling age retention requires content-tags-only=true")
    if retention_days is not None and (
        type(retention_days) is not int or retention_days <= 0
    ):
        raise ValueError("Retention days must be a positive integer")
    if not TAG_PATTERN.fullmatch(keep_tag):
        raise ValueError("Invalid KEEP_TAG")
    if content_tags_only and not re.fullmatch(r"[0-9a-f]{7}", keep_tag):
        raise ValueError("KEEP_TAG must be a seven-character content hash")

    if retention_days is not None:
        try:
            cutoff = now - dt.timedelta(days=retention_days)
        except OverflowError as exc:
            raise ValueError("Retention days exceed the supported date range") from exc

    versions = client.versions()
    keep_tags = {"latest", keep_tag}
    created_by_tag = {
        tag: parse_created_at(version.get("created_at"))
        for tag, version in versions.items()
    }
    if retention_days is not None:
        dated_tags = [
            tag for tag, created in created_by_tag.items() if created is not None
        ]
        if dated_tags:
            newest = max(dated_tags, key=lambda tag: created_by_tag[tag])
            keep_tags.add(newest)
            print(f"Keeping newest tag defensively: {newest}")

    deleted = 0
    for tag, version in sorted(versions.items()):
        if tag in keep_tags:
            continue
        if content_tags_only and not re.fullmatch(r"[0-9a-f]{7}", tag):
            continue
        if retention_days is not None:
            created = created_by_tag[tag]
            if created is None or created >= cutoff:
                print(f"Keeping tag {tag}: recent or unknown creation time")
                continue
        try:
            current = client.get(tag)
            if not isinstance(current, dict):
                raise TypeError("Invalid package version response")
            if (
                current.get("id") != version["id"]
                or current.get("name") != client.name
                or current.get("type") != "container"
                or current.get("version") != tag
            ):
                print(f"Keeping tag {tag}: package version changed")
                continue
            client.delete(tag)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            print(f"Skipping tag {tag}: already removed")
            continue
        deleted += 1
        print(f"Deleted tag {tag}")
    print(f"Cleanup complete; deleted {deleted} tag(s)")
    return 0


def main():
    try:
        image = urllib.parse.urlsplit("https://" + os.environ["IMAGE_NAME"])
        if (
            not image.hostname
            or image.username
            or image.password
            or image.query
            or image.fragment
        ):
            raise ValueError("Invalid IMAGE_NAME")
        owner, name = image.path.strip("/").split("/", 1)
        if not owner or not name:
            raise ValueError("IMAGE_NAME must contain an owner and package name")
        token = os.environ["FORGEJO_TOKEN"]
        if not token or not all("!" <= character <= "~" for character in token):
            raise ValueError("Token must be nonempty printable ASCII without spaces")
        raw_days = os.environ["GC_RETENTION_DAYS"].strip().lower()
        if raw_days != "none" and not re.fullmatch(r"[0-9]+", raw_days):
            raise ValueError("Retention days must be a positive integer or none")
        raw_content_only = (
            os.environ.get("GC_CONTENT_TAGS_ONLY", "false").strip().lower()
        )
        if raw_content_only not in {"true", "false"}:
            raise ValueError("content-tags-only must be true or false")
        client = ForgejoPackageClient(f"https://{image.netloc}", owner, name, token)
        return cleanup(
            client,
            os.environ["KEEP_TAG"],
            dt.datetime.now(dt.UTC),
            retention_days=None if raw_days == "none" else int(raw_days),
            content_tags_only=raw_content_only == "true",
        )
    except (KeyError, TypeError, ValueError, OSError) as exc:
        print(f"Registry cleanup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
