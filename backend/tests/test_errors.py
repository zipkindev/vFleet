from app.errors import humanize_vcenter_error, is_not_authenticated


NGINX_404 = """
<html>
<head><title>404 Not Found</title></head>
<body>
<center><h1>404 Not Found</h1></center>
<hr><center>nginx/1.29.8</center>
</body>
</html>
"""

NOT_AUTH = (
    "(vim.fault.NotAuthenticated) {\n"
    "   msg = 'The session is not authenticated.',\n"
    "   object = 'vim.view.ContainerView:session[abc]def',\n"
    "}"
)


def test_humanize_nginx_404_mentions_api_path():
    text = humanize_vcenter_error(NGINX_404, host="vcn02.example.com")
    assert text == (
        "vCenter (vcn02.example.com) answered HTTP 404 for the API path "
        "(UI may still work). Check host/port or try again after VPN settles."
    )
    assert "<html" not in text.lower()
    assert "nginx" not in text.lower()


def test_humanize_not_authenticated():
    assert is_not_authenticated(NOT_AUTH)
    text = humanize_vcenter_error(NOT_AUTH, host="vc.lab")
    assert "session expired" in text.lower()
    assert "vim.fault" not in text
    assert "ContainerView" not in text


def test_humanize_soap_msg_field():
    raw = "(vim.fault.InvalidName) { msg = 'The name is already in use.', }"
    assert humanize_vcenter_error(raw) == "The name is already in use"


def test_humanize_no_permission_includes_privilege():
    raw = (
        "(vim.fault.NoPermission) { msg = 'Permission to perform this operation was denied.', "
        "privilegeId = 'Resource.ColdMigrate' }"
    )
    text = humanize_vcenter_error(raw)
    assert "Resource.ColdMigrate" in text
    assert "vim.fault" not in text
    assert "dynamicType" not in text


def test_permission_denied_is_permanent():
    from app.errors import PermanentError, is_permanent, is_transient

    assert is_permanent(Exception("Permission to perform this operation was denied."))
    assert is_permanent(PermanentError("vCenter denied Resource.ColdMigrate."))
    assert not is_transient(Exception("vim.fault.NoPermission privilegeId = 'Resource.ColdMigrate'"))
