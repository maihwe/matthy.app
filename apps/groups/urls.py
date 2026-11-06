from django.urls import path

from apps.groups import views

urlpatterns = [
    path("", views.GroupListView.as_view(), name="group-list"),
    path("invitations/accept/", views.InvitationAcceptView.as_view(), name="invitation-accept"),
    path("invitations/decline/", views.InvitationDeclineView.as_view(), name="invitation-decline"),
    path("<uuid:public_id>/", views.GroupDetailView.as_view(), name="group-detail"),
    path("<uuid:public_id>/archive/", views.GroupArchiveView.as_view(), name="group-archive"),
    path(
        "<uuid:public_id>/transfer-ownership/",
        views.GroupTransferView.as_view(),
        name="group-transfer",
    ),
    path("<uuid:public_id>/join/", views.GroupJoinView.as_view(), name="group-join"),
    path("<uuid:public_id>/leave/", views.GroupLeaveView.as_view(), name="group-leave"),
    path("<uuid:public_id>/members/", views.GroupMembersView.as_view(), name="group-members"),
    path("<uuid:public_id>/members/invite/", views.GroupInviteView.as_view(), name="group-invite"),
    path(
        "<uuid:public_id>/members/<uuid:member_id>/approve/",
        views.MemberActionView.as_view(),
        name="member-approve",
    ),
    path(
        "<uuid:public_id>/members/<uuid:member_id>/suspend/",
        views.MemberSuspendView.as_view(),
        name="member-suspend",
    ),
    path(
        "<uuid:public_id>/members/<uuid:member_id>/reinstate/",
        views.MemberReinstateView.as_view(),
        name="member-reinstate",
    ),
    path(
        "<uuid:public_id>/members/<uuid:member_id>/remove/",
        views.MemberRemoveView.as_view(),
        name="member-remove",
    ),
    path(
        "<uuid:public_id>/members/<uuid:member_id>/history/",
        views.MemberHistoryView.as_view(),
        name="member-history",
    ),
    path(
        "<uuid:public_id>/members/<uuid:member_id>/roles/",
        views.MemberRolesView.as_view(),
        name="member-role-assign",
    ),
    path(
        "<uuid:public_id>/members/<uuid:member_id>/roles/<uuid:role_id>/",
        views.MemberRoleRevokeView.as_view(),
        name="member-role-revoke",
    ),
    path("<uuid:public_id>/roles/", views.GroupRolesView.as_view(), name="group-roles"),
    path(
        "<uuid:public_id>/roles/<uuid:role_id>/",
        views.GroupRoleDetailView.as_view(),
        name="group-role-detail",
    ),
    path(
        "<uuid:public_id>/roles/<uuid:role_id>/permissions/",
        views.GroupRolePermissionsView.as_view(),
        name="role-permissions",
    ),
    path("<uuid:public_id>/settings/", views.GroupSettingsView.as_view(), name="group-settings"),
    path("<uuid:public_id>/rules/", views.GroupRulesView.as_view(), name="group-rules"),
    path("<uuid:public_id>/dashboard/", views.GroupDashboardView.as_view(), name="group-dashboard"),
]
