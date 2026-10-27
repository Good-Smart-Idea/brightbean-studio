"""Pure same-account native-video configuration projection; no provider calls."""

from providers.meta_video import facebook_native_configuration, instagram_native_configuration


def instagram_native_credentials(account):
    if account.platform != "instagram":
        raise ValueError("Native Instagram video upload requires a Facebook-login account.")
    expiry = account.facebook_user_token_expires_at
    return {
        "ig_user_id": account.account_platform_id,
        "facebook_user_access_token": account.oauth_facebook_user_access_token,
        "facebook_user_token_expires_at": expiry.timestamp() if expiry is not None else None,
        "native_video_account_connected": not account.needs_reconnect and not account.workspace.is_archived,
    }


def native_video_configuration(account):
    if account.platform == "instagram":
        return instagram_native_configuration(instagram_native_credentials(account))
    if account.platform == "facebook":
        return facebook_native_configuration(
            {
                "page_id": account.account_platform_id,
                "native_video_account_connected": not account.needs_reconnect and not account.workspace.is_archived,
            },
            account.oauth_access_token,
        )
    return {
        "supported": False,
        "configured": False,
        "blocker": "unsupported_platform",
        "provider_eligibility_verified": False,
    }
