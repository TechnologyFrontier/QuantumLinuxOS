"""OS-02: authorisation by kernel peer credentials and the quantum group."""

import grp
import os
import pwd
from types import SimpleNamespace

import pytest

from quantumos import service
from quantumos.service import Authorizer, ServiceConfig

QUANTUM_GID = 5000
USERS = {
    1001: SimpleNamespace(pw_name="alice", pw_gid=1001),  # supplementary member
    1002: SimpleNamespace(pw_name="bob", pw_gid=1002),  # not a member
    1003: SimpleNamespace(pw_name="carol", pw_gid=QUANTUM_GID),  # primary group
}


@pytest.fixture
def fake_db(monkeypatch):
    def getgrnam(name):
        if name != "quantum":
            raise KeyError(name)
        return SimpleNamespace(gr_gid=QUANTUM_GID, gr_mem=["alice"])

    def getpwuid(uid):
        try:
            return USERS[uid]
        except KeyError:
            raise KeyError(uid)

    def getgrouplist(name, gid):
        return [gid] + ([QUANTUM_GID] if name == "alice" else [])

    monkeypatch.setattr(service.grp, "getgrnam", getgrnam)
    monkeypatch.setattr(service.pwd, "getpwuid", getpwuid)
    monkeypatch.setattr(service.os, "getgrouplist", getgrouplist)


@pytest.mark.parametrize("uid,gid,allowed", [
    (1001, 1001, True),
    (1002, 1002, False),
    (1003, QUANTUM_GID, True),
    (1002, QUANTUM_GID, True),  # process running with quantum as its gid (newgrp/sg)
    (0, 0, False),  # root is not implicitly a member
    (4242, 4242, False),  # unknown uid
])
def test_group_membership(fake_db, uid, gid, allowed):
    assert Authorizer(ServiceConfig()).is_allowed(uid, gid) is allowed


def test_missing_group_denies_everyone(monkeypatch, fake_db):
    a = Authorizer(ServiceConfig(allowed_group="no-such-group"))
    assert not a.is_allowed(1001, 1001)


def test_explicit_uid_allow_list(fake_db):
    a = Authorizer(ServiceConfig(allowed_group=None, allowed_uids=[1002]))
    assert a.is_allowed(1002, 1002) and not a.is_allowed(1001, 1001)
