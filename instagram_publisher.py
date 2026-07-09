"""
Publikacja zdjęcia na Instagramie przez Instagram Graph API (Content Publishing API).

Flow (dla pojedynczego zdjęcia):
1. POST /{ig-user-id}/media  z image_url + caption  -> zwraca creation_id (kontener)
2. (opcjonalnie) GET /{creation_id}?fields=status_code  -> czekamy aż status = FINISHED
3. POST /{ig-user-id}/media_publish  z creation_id  -> publikuje na Instagramie

Wymaga:
- IG_USER_ID   - numeryczne ID konta Instagram Business/Creator (NIE nazwa użytkownika)
- IG_ACCESS_TOKEN - długożyjący token strony (Page Access Token) z uprawnieniami
                     instagram_basic, instagram_content_publish, pages_show_list
- Obrazek musi być dostępny pod publicznym adresem URL (Instagram sam go pobiera).
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


def publish_image(ig_user_id: str, access_token: str, image_url: str, caption: str,
                   poll_attempts: int = 10, poll_delay_seconds: float = 2.0) -> str:
    """
    Publikuje pojedyncze zdjęcie na Instagramie. Zwraca ID opublikowanego media.
    Podnosi InstagramPublishError w razie problemu (np. zły token, obrazek niedostępny).
    """
    create_resp = requests.post(
        f"{GRAPH_API_BASE}/{ig_user_id}/media",
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": access_token,
        },
        timeout=30,
    )
    creation_id = _check_response(create_resp)["id"]

    for _ in range(poll_attempts):
        status_resp = requests.get(
            f"{GRAPH_API_BASE}/{creation_id}",
            params={"fields": "status_code", "access_token": access_token},
            timeout=30,
        )
        status = _check_response(status_resp).get("status_code")
        if status == "FINISHED":
            break
        if status == "ERROR":
            raise InstagramPublishError("Instagram nie zdołał przetworzyć obrazka (status ERROR).")
        time.sleep(poll_delay_seconds)
    else:
        raise InstagramPublishError("Przekroczono czas oczekiwania na przetworzenie obrazka przez Instagram.")

    publish_resp = requests.post(
        f"{GRAPH_API_BASE}/{ig_user_id}/media_publish",
        data={
            "creation_id": creation_id,
            "access_token": access_token,
        },
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
