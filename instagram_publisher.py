"""
Publikacja zdjęcia (pojedynczego lub karuzeli) na Instagramie przez Instagram
Graph API (Content Publishing API).

Pojedyncze zdjęcie:
1. POST /{ig-user-id}/media  z image_url + caption  -> creation_id
2. GET /{creation_id}?fields=status_code  -> czekamy aż status = FINISHED
3. POST /{ig-user-id}/media_publish  z creation_id  -> publikuje

Karuzela (2-10 zdjęć):
1. Dla każdego zdjęcia: POST /{ig-user-id}/media z image_url + is_carousel_item=true -> child id
2. POST /{ig-user-id}/media z media_type=CAROUSEL + children=... + caption -> creation_id
3. GET .../status_code -> czekamy FINISHED
4. POST /{ig-user-id}/media_publish z creation_id -> publikuje

Wymaga:
- IG_USER_ID   - numeryczne ID konta Instagram Business/Creator
- IG_ACCESS_TOKEN - długożyjący token strony z uprawnieniami
                     instagram_basic, instagram_content_publish, pages_show_list
- Obrazki muszą być dostępne pod publicznym adresem URL.
"""
import time
import requests

GRAPH_API_VERSION = "v22.0"
GRAPH_API_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"


class InstagramPublishError(Exception):
    pass


def _check_response(resp: requests.Response):
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            detail = resp.text
        raise InstagramPublishError(f"Instagram API error ({resp.status_code}): {detail}")
    return resp.json()


def _wait_until_finished(creation_id, access_token, poll_attempts, poll_delay_seconds):
    for _ in range(poll_attempts):
        status_resp = requests.get(
            f"{GRAPH_API_BASE}/{creation_id}",
            params={"fields": "status_code", "access_token": access_token},
            timeout=30,
        )
        status = _check_response(status_resp).get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise InstagramPublishError("Instagram nie zdołał przetworzyć mediów (status ERROR).")
        time.sleep(poll_delay_seconds)
    raise InstagramPublishError("Przekroczono czas oczekiwania na przetworzenie mediów przez Instagram.")


def publish_image(ig_user_id: str, access_token: str, image_url: str, caption: str,
                   poll_attempts: int = 10, poll_delay_seconds: float = 2.0) -> str:
    """Publikuje pojedyncze zdjęcie. Zwraca ID opublikowanego media."""
    create_resp = requests.post(
        f"{GRAPH_API_BASE}/{ig_user_id}/media",
        data={"image_url": image_url, "caption": caption, "access_token": access_token},
        timeout=30,
    )
    creation_id = _check_response(create_resp)["id"]

    _wait_until_finished(creation_id, access_token, poll_attempts, poll_delay_seconds)

    publish_resp = requests.post(
        f"{GRAPH_API_BASE}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=30,
    )
    return _check_response(publish_resp)["id"]


def _create_carousel_item(ig_user_id: str, access_token: str, image_url: str) -> str:
    resp = requests.post(
        f"{GRAPH_API_BASE}/{ig_user_id}/media",
        data={"image_url": image_url, "is_carousel_item": "true", "access_token": access_token},
        timeout=30,
    )
    return _check_response(resp)["id"]


def publish_carousel(ig_user_id: str, access_token: str, image_urls: list[str], caption: str,
                      poll_attempts: int = 10, poll_delay_seconds: float = 2.0) -> str:
    """
    Publikuje karuzelę (2-10 zdjęć w jednym poście). Zwraca ID opublikowanego media.
    """
    if not (2 <= len(image_urls) <= 10):
        raise InstagramPublishError("Karuzela musi mieć od 2 do 10 zdjęć.")

    child_ids = [_create_carousel_item(ig_user_id, access_token, url) for url in image_urls]

    create_resp = requests.post(
        f"{GRAPH_API_BASE}/{ig_user_id}/media",
        data={
            "media_type": "CAROUSEL",
            "caption": caption,
            "children": ",".join(child_ids),
            "access_token": access_token,
        },
        timeout=30,
    )
    creation_id = _check_response(create_resp)["id"]

    _wait_until_finished(creation_id, access_token, poll_attempts, poll_delay_seconds)

    publish_resp = requests.post(
        f"{GRAPH_API_BASE}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=30,
    )
    return _check_response(publish_resp)["id"]


def get_permalink(media_id: str, access_token: str) -> str | None:
    """Zwraca link do opublikowanego posta na Instagramie (może się nie udać - wtedy None)."""
    try:
        resp = requests.get(
            f"{GRAPH_API_BASE}/{media_id}",
            params={"fields": "permalink", "access_token": access_token},
            timeout=15,
        )
        return _check_response(resp).get("permalink")
    except (InstagramPublishError, requests.RequestException):
        return None
