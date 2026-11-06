-- Hard rules and invalidation live below every ORM/admin/API write path.
CREATE FUNCTION groups_touch_user(subject bigint) RETURNS void AS $$
DECLARE needed boolean;
BEGIN
  PERFORM id FROM users WHERE id = subject FOR UPDATE;
  SELECT EXISTS (
    SELECT 1 FROM platform_role_assignments pa JOIN group_roles r ON r.id=pa.role_id
    WHERE pa.user_id=subject AND r.code IN ('SUPER_ADMIN','PLATFORM_ADMIN')
  ) OR EXISTS (
    SELECT 1 FROM group_member_roles a JOIN group_members m ON m.id=a.group_member_id
    JOIN group_roles r ON r.id=a.group_role_id JOIN role_templates t ON t.id=r.template_id
    WHERE m.user_id=subject AND m.status='ACTIVE'
      AND t.code IN ('GROUP_OWNER','CHAIRMAN','TREASURER')
  ) INTO needed;
  UPDATE users SET authorization_version=authorization_version+1, role_mfa_required=needed WHERE id=subject;
  UPDATE user_sessions SET revoked_at=COALESCE(revoked_at,clock_timestamp()), revoked_reason='REVOKED'
    WHERE user_id=subject AND (revoked_at IS NULL OR revoked_reason='ROTATED');
END; $$ LANGUAGE plpgsql;

CREATE FUNCTION groups_invalidate() RETURNS trigger AS $$
DECLARE scope_id bigint; subject bigint; affected bigint;
BEGIN
  IF TG_TABLE_NAME='group_members' THEN
    scope_id := COALESCE(NEW.group_id,OLD.group_id);
    subject := COALESCE(NEW.user_id,OLD.user_id);
    IF TG_OP='UPDATE' AND NEW.status=OLD.status THEN RETURN NEW; END IF;
    IF COALESCE(NEW.status,'')='ACTIVE' OR COALESCE(OLD.status,'')='ACTIVE' THEN
      PERFORM groups_touch_user(subject);
    END IF;
  ELSIF TG_TABLE_NAME='group_member_roles' THEN
    SELECT group_id,user_id INTO scope_id,subject FROM group_members WHERE id=COALESCE(NEW.group_member_id,OLD.group_member_id);
    IF EXISTS (SELECT 1 FROM group_members WHERE id=COALESCE(NEW.group_member_id,OLD.group_member_id) AND status='ACTIVE') THEN
      PERFORM groups_touch_user(subject);
    END IF;
  ELSIF TG_TABLE_NAME='platform_role_assignments' THEN
    subject:=COALESCE(NEW.user_id,OLD.user_id);
    PERFORM groups_touch_user(subject);
  ELSIF TG_TABLE_NAME='group_permissions' THEN
    IF TG_OP='UPDATE' AND NEW.granted=OLD.granted THEN RETURN NEW; END IF;
    SELECT group_id INTO scope_id FROM group_roles WHERE id=COALESCE(NEW.group_role_id,OLD.group_role_id);
    FOR affected IN SELECT DISTINCT m.user_id FROM group_member_roles a JOIN group_members m ON m.id=a.group_member_id
      WHERE a.group_role_id=COALESCE(NEW.group_role_id,OLD.group_role_id) AND m.status='ACTIVE' ORDER BY m.user_id
    LOOP PERFORM groups_touch_user(affected); END LOOP;
    FOR affected IN SELECT user_id FROM platform_role_assignments WHERE role_id=COALESCE(NEW.group_role_id,OLD.group_role_id) ORDER BY user_id
    LOOP PERFORM groups_touch_user(affected); END LOOP;
  ELSE
    scope_id:=COALESCE(NEW.group_id,OLD.group_id);
  END IF;
  IF scope_id IS NULL THEN
    UPDATE platform_permission_state SET version=version+1 WHERE id=1;
  ELSE
    UPDATE groups SET permission_version=permission_version+1 WHERE id=scope_id;
  END IF;
  RETURN COALESCE(NEW,OLD);
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER groups_member_version AFTER INSERT OR UPDATE OR DELETE ON group_members FOR EACH ROW EXECUTE FUNCTION groups_invalidate();
CREATE TRIGGER groups_assignment_version AFTER INSERT OR UPDATE OR DELETE ON group_member_roles FOR EACH ROW EXECUTE FUNCTION groups_invalidate();
CREATE TRIGGER groups_role_version AFTER INSERT OR UPDATE OR DELETE ON group_roles FOR EACH ROW EXECUTE FUNCTION groups_invalidate();
CREATE TRIGGER groups_grant_version AFTER INSERT OR UPDATE OR DELETE ON group_permissions FOR EACH ROW EXECUTE FUNCTION groups_invalidate();
CREATE TRIGGER groups_platform_version AFTER INSERT OR UPDATE OR DELETE ON platform_role_assignments FOR EACH ROW EXECUTE FUNCTION groups_invalidate();

CREATE FUNCTION groups_membership_history() RETURNS trigger AS $$
BEGIN
  IF TG_OP='INSERT' OR NEW.status IS DISTINCT FROM OLD.status THEN
    INSERT INTO group_member_history(group_member_id,from_status,to_status,changed_by_id,reason,occurred_at)
      VALUES(NEW.id,CASE WHEN TG_OP='INSERT' THEN '' ELSE OLD.status END,NEW.status,
        COALESCE(NEW.status_changed_by_id,NEW.user_id),COALESCE(NEW.suspension_reason,''),clock_timestamp());
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER groups_member_history AFTER INSERT OR UPDATE ON group_members FOR EACH ROW EXECUTE FUNCTION groups_membership_history();

CREATE FUNCTION groups_keep_history() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'group history is append-only' USING ERRCODE='23514'; END; $$ LANGUAGE plpgsql;
CREATE TRIGGER groups_history_immutable BEFORE UPDATE OR DELETE ON group_member_history FOR EACH ROW EXECUTE FUNCTION groups_keep_history();
CREATE TRIGGER groups_no_member_delete BEFORE DELETE ON group_members FOR EACH ROW EXECUTE FUNCTION groups_keep_history();
CREATE TRIGGER groups_no_group_delete BEFORE DELETE ON groups FOR EACH ROW EXECUTE FUNCTION groups_keep_history();

CREATE FUNCTION groups_guard_role() RETURNS trigger AS $$
DECLARE template_scope text;
BEGIN
  IF TG_OP='DELETE' THEN
    IF OLD.is_system THEN RAISE EXCEPTION 'system roles cannot be deleted' USING ERRCODE='23514'; END IF;
    RETURN OLD;
  END IF;
  SELECT scope INTO template_scope FROM role_templates WHERE id=NEW.template_id;
  IF (NEW.group_id IS NULL) IS DISTINCT FROM (template_scope='PLATFORM') THEN
    RAISE EXCEPTION 'role template has the wrong scope' USING ERRCODE='23514';
  END IF;
  IF TG_OP='UPDATE' AND (NEW.group_id,NEW.template_id,NEW.code,NEW.is_system,NEW.created_at) IS DISTINCT FROM (OLD.group_id,OLD.template_id,OLD.code,OLD.is_system,OLD.created_at) THEN
    RAISE EXCEPTION 'role identity and safety bounds are immutable' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER groups_role_guard BEFORE INSERT OR UPDATE OR DELETE ON group_roles FOR EACH ROW EXECUTE FUNCTION groups_guard_role();

CREATE FUNCTION groups_guard_grant() RETURNS trigger AS $$
DECLARE policy text; scope_id bigint; born timestamptz; actor bigint;
BEGIN
  SELECT r.group_id,r.created_at,t.policies->>p.code INTO scope_id,born,policy
    FROM group_roles r JOIN role_templates t ON t.id=r.template_id CROSS JOIN permissions p
    WHERE r.id=COALESCE(NEW.group_role_id,OLD.group_role_id) AND p.id=COALESCE(NEW.permission_id,OLD.permission_id);
  IF TG_OP='DELETE' THEN
    IF policy IN ('required','dual') THEN RAISE EXCEPTION 'required grant cannot be removed' USING ERRCODE='23514'; END IF;
    RETURN OLD;
  END IF;
  IF TG_OP='UPDATE' AND (NEW.group_role_id,NEW.permission_id) IS DISTINCT FROM (OLD.group_role_id,OLD.permission_id) THEN
    RAISE EXCEPTION 'permission binding cannot be moved' USING ERRCODE='23514';
  END IF;
  IF NEW.granted AND policy IS NULL THEN RAISE EXCEPTION 'forbidden permission grant' USING ERRCODE='23514'; END IF;
  IF NOT NEW.granted AND policy IN ('required','dual') THEN RAISE EXCEPTION 'required grant cannot be disabled' USING ERRCODE='23514'; END IF;
  actor:=NEW.changed_by_id;
  IF scope_id IS NOT NULL THEN
    PERFORM id FROM groups WHERE id=scope_id FOR UPDATE;
    IF actor IS NULL THEN
      IF TG_OP<>'INSERT' OR born<transaction_timestamp() THEN
        RAISE EXCEPTION 'group permission changes require an actor' USING ERRCODE='23514';
      END IF;
    ELSE
      IF NOT EXISTS(SELECT 1 FROM group_members m JOIN group_member_roles a ON a.group_member_id=m.id
        JOIN group_permissions p ON p.group_role_id=a.group_role_id JOIN permissions c ON c.id=p.permission_id
        JOIN users u ON u.id=m.user_id WHERE m.group_id=scope_id AND m.user_id=actor AND m.status='ACTIVE'
          AND u.account_status='ACTIVE' AND u.two_factor_enabled AND c.code='groups.manage_settings' AND p.granted) THEN
        RAISE EXCEPTION 'permission editor is not authorised' USING ERRCODE='23514';
      END IF;
      IF NEW.granted AND (TG_OP='INSERT' OR NOT OLD.granted) AND EXISTS(SELECT 1 FROM group_members m JOIN group_member_roles a ON a.group_member_id=m.id WHERE m.group_id=scope_id AND m.user_id=actor AND m.status='ACTIVE' AND a.group_role_id=NEW.group_role_id) THEN
        RAISE EXCEPTION 'cannot grant oneself a permission' USING ERRCODE='23514';
      END IF;
    END IF;
  ELSE
    PERFORM id FROM platform_permission_state WHERE id=1 FOR UPDATE;
    IF actor IS NULL AND policy='optional' AND NEW.granted THEN
      RAISE EXCEPTION 'optional platform grants require an actor' USING ERRCODE='23514';
    END IF;
    IF actor IS NOT NULL THEN
      IF NOT EXISTS(SELECT 1 FROM platform_role_assignments a JOIN group_roles r ON r.id=a.role_id JOIN users u ON u.id=a.user_id
        WHERE a.user_id=actor AND r.code='SUPER_ADMIN' AND u.account_status='ACTIVE' AND u.two_factor_enabled) THEN
        RAISE EXCEPTION 'platform grant editor is not authorised' USING ERRCODE='23514';
      END IF;
      IF NEW.granted AND (TG_OP='INSERT' OR NOT OLD.granted) AND EXISTS(SELECT 1 FROM platform_role_assignments WHERE user_id=actor AND role_id=NEW.group_role_id) THEN
        RAISE EXCEPTION 'cannot grant oneself a platform permission' USING ERRCODE='23514';
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER groups_grant_guard BEFORE INSERT OR UPDATE OR DELETE ON group_permissions FOR EACH ROW EXECUTE FUNCTION groups_guard_grant();

CREATE FUNCTION groups_guard_assignment() RETURNS trigger AS $$
DECLARE scope_id bigint; subject bigint; owner bigint; creator bigint; born timestamptz; role_group bigint; system_role boolean; basis bigint;
BEGIN
  SELECT m.group_id,m.user_id,g.owner_id,g.created_by_id,g.created_at INTO scope_id,subject,owner,creator,born
    FROM group_members m JOIN groups g ON g.id=m.group_id WHERE m.id=NEW.group_member_id;
  PERFORM id FROM groups WHERE id=scope_id FOR UPDATE;
  SELECT group_id,is_system,template_id INTO role_group,system_role,basis FROM group_roles WHERE id=NEW.group_role_id;
  IF role_group IS DISTINCT FROM scope_id THEN RAISE EXCEPTION 'cross-group role assignment' USING ERRCODE='23514'; END IF;
  IF TG_OP='UPDATE' AND (NEW.group_member_id,NEW.group_role_id,NEW.assigned_by_id,NEW.bootstrap) IS DISTINCT FROM (OLD.group_member_id,OLD.group_role_id,OLD.assigned_by_id,OLD.bootstrap) THEN
    RAISE EXCEPTION 'role assignment identity is immutable' USING ERRCODE='23514';
  END IF;
  IF EXISTS(SELECT 1 FROM group_roles r JOIN role_templates t ON t.id=r.template_id JOIN group_members m ON m.id=NEW.group_member_id
    WHERE r.id=NEW.group_role_id AND r.code='MEMBER' AND t.code='MEMBER' AND r.is_system AND m.status='ACTIVE') THEN
    -- MEMBER is the structural baseline on an approved/accepted membership.
    RETURN NEW;
  ELSIF NEW.bootstrap THEN
    IF TG_OP<>'INSERT' OR born<transaction_timestamp() OR NEW.assigned_by_id<>creator OR owner<>creator
      OR NOT EXISTS(SELECT 1 FROM groups WHERE id=scope_id AND status='DRAFT') THEN
      RAISE EXCEPTION 'role bootstrap is limited to initial group creation' USING ERRCODE='23514';
    END IF;
    -- Only the initial owner/MEMBER bootstrap may target the founder.
    IF subject=creator AND NOT EXISTS(SELECT 1 FROM group_roles WHERE id=NEW.group_role_id AND code IN ('GROUP_OWNER','MEMBER')) THEN
      RAISE EXCEPTION 'cannot bootstrap oneself additional permissions' USING ERRCODE='23514';
    END IF;
  ELSE
    IF subject=NEW.assigned_by_id THEN RAISE EXCEPTION 'cannot assign oneself a role' USING ERRCODE='23514'; END IF;
    IF NOT EXISTS(SELECT 1 FROM group_members m JOIN group_member_roles a ON a.group_member_id=m.id
      JOIN group_permissions p ON p.group_role_id=a.group_role_id JOIN permissions c ON c.id=p.permission_id
      JOIN users u ON u.id=m.user_id WHERE m.group_id=scope_id AND m.user_id=NEW.assigned_by_id AND m.status='ACTIVE'
        AND u.account_status='ACTIVE' AND c.code='members.assign_roles' AND p.granted) THEN
      RAISE EXCEPTION 'role assigner is not authorised' USING ERRCODE='23514';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM group_members m JOIN group_member_roles a ON a.group_member_id=m.id
      JOIN group_roles held ON held.id=a.group_role_id WHERE m.group_id=scope_id AND m.user_id=NEW.assigned_by_id
        AND m.status='ACTIVE' AND (held.id=NEW.group_role_id OR (NOT system_role AND held.template_id=basis))) THEN
      RAISE EXCEPTION 'cannot grant a role the assigner does not hold' USING ERRCODE='23514';
    END IF;
    IF NOT system_role AND EXISTS(SELECT 1 FROM group_permissions target WHERE target.group_role_id=NEW.group_role_id AND target.granted
      AND NOT EXISTS(SELECT 1 FROM group_members m JOIN group_member_roles a ON a.group_member_id=m.id JOIN group_permissions held ON held.group_role_id=a.group_role_id
        WHERE m.group_id=scope_id AND m.user_id=NEW.assigned_by_id AND m.status='ACTIVE' AND held.permission_id=target.permission_id AND held.granted)) THEN
      RAISE EXCEPTION 'custom role exceeds assigner authority' USING ERRCODE='23514';
    END IF;
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER groups_assignment_guard BEFORE INSERT OR UPDATE ON group_member_roles FOR EACH ROW EXECUTE FUNCTION groups_guard_assignment();

CREATE FUNCTION groups_guard_platform_assignment() RETURNS trigger AS $$
BEGIN
  PERFORM id FROM platform_permission_state WHERE id=1 FOR UPDATE;
  IF NOT EXISTS(SELECT 1 FROM group_roles r JOIN role_templates t ON t.id=r.template_id WHERE r.id=NEW.role_id AND r.group_id IS NULL AND t.scope='PLATFORM') THEN
    RAISE EXCEPTION 'platform assignment requires a platform role' USING ERRCODE='23514';
  END IF;
  IF TG_OP='UPDATE' THEN RAISE EXCEPTION 'platform assignments are immutable' USING ERRCODE='23514'; END IF;
  IF NEW.user_id=NEW.assigned_by_id THEN RAISE EXCEPTION 'cannot assign oneself a platform role' USING ERRCODE='23514'; END IF;
  IF NEW.assigned_by_id IS NULL THEN
    IF NOT EXISTS(SELECT 1 FROM group_roles WHERE id=NEW.role_id AND code='SUPER_ADMIN')
      OR EXISTS(SELECT 1 FROM platform_role_assignments pa JOIN group_roles r ON r.id=pa.role_id WHERE r.code='SUPER_ADMIN')
      OR NOT EXISTS(SELECT 1 FROM users WHERE id=NEW.user_id AND is_superuser AND account_status='ACTIVE' AND two_factor_enabled) THEN
      RAISE EXCEPTION 'only the first verified super admin can be bootstrapped' USING ERRCODE='23514';
    END IF;
  ELSIF NOT EXISTS(SELECT 1 FROM platform_role_assignments pa JOIN group_roles r ON r.id=pa.role_id JOIN users u ON u.id=pa.user_id WHERE pa.user_id=NEW.assigned_by_id AND r.code='SUPER_ADMIN' AND u.account_status='ACTIVE' AND u.two_factor_enabled) THEN
    RAISE EXCEPTION 'only super admin can assign platform roles' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER groups_platform_assignment_guard BEFORE INSERT OR UPDATE ON platform_role_assignments FOR EACH ROW EXECUTE FUNCTION groups_guard_platform_assignment();

CREATE FUNCTION groups_keep_owner() RETURNS trigger AS $$
DECLARE scope_id bigint;
BEGIN
  IF TG_TABLE_NAME='groups' THEN scope_id:=COALESCE(NEW.id,OLD.id);
  ELSIF TG_TABLE_NAME='group_members' THEN scope_id:=COALESCE(NEW.group_id,OLD.group_id);
  ELSE SELECT group_id INTO scope_id FROM group_members WHERE id=COALESCE(NEW.group_member_id,OLD.group_member_id); END IF;
  IF EXISTS(SELECT 1 FROM groups g WHERE g.id=scope_id AND NOT EXISTS(
    SELECT 1 FROM group_members m JOIN group_member_roles a ON a.group_member_id=m.id
    JOIN group_roles r ON r.id=a.group_role_id WHERE m.group_id=g.id AND m.user_id=g.owner_id AND m.status='ACTIVE' AND r.code='GROUP_OWNER' AND r.group_id=g.id
  )) THEN RAISE EXCEPTION 'ownership must be transferred before removing or demoting the owner' USING ERRCODE='23514'; END IF;
  RETURN NULL;
END; $$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER groups_owner_present AFTER INSERT OR UPDATE ON groups DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION groups_keep_owner();
CREATE CONSTRAINT TRIGGER groups_member_owner AFTER UPDATE OR DELETE ON group_members DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION groups_keep_owner();
CREATE CONSTRAINT TRIGGER groups_assignment_owner AFTER UPDATE OR DELETE ON group_member_roles DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION groups_keep_owner();

CREATE FUNCTION groups_keep_super_admin() RETURNS trigger AS $$
BEGIN
  IF EXISTS(SELECT 1 FROM group_roles WHERE id=OLD.role_id AND code='SUPER_ADMIN') AND NOT EXISTS(
    SELECT 1 FROM platform_role_assignments a JOIN group_roles r ON r.id=a.role_id WHERE r.code='SUPER_ADMIN'
  ) THEN RAISE EXCEPTION 'the last super admin is protected' USING ERRCODE='23514'; END IF;
  RETURN NULL;
END; $$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER groups_super_admin_present AFTER DELETE ON platform_role_assignments DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION groups_keep_super_admin();

CREATE FUNCTION groups_guard_identity() RETURNS trigger AS $$
BEGIN
  IF TG_TABLE_NAME='group_members' THEN
    IF (NEW.group_id,NEW.user_id) IS DISTINCT FROM (OLD.group_id,OLD.user_id) THEN
      RAISE EXCEPTION 'membership identity is immutable' USING ERRCODE='23514';
    END IF;
  ELSE
    IF (NEW.created_by_id,NEW.created_at) IS DISTINCT FROM (OLD.created_by_id,OLD.created_at) THEN
      RAISE EXCEPTION 'group creation identity is immutable' USING ERRCODE='23514';
    END IF;
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER groups_identity_guard BEFORE UPDATE ON groups FOR EACH ROW EXECUTE FUNCTION groups_guard_identity();
CREATE TRIGGER groups_member_identity_guard BEFORE UPDATE ON group_members FOR EACH ROW EXECUTE FUNCTION groups_guard_identity();

CREATE FUNCTION groups_guard_catalogue() RETURNS trigger AS $$
BEGIN
  IF TG_TABLE_NAME='role_templates' THEN
    IF (NEW.code,NEW.scope,NEW.policies) IS DISTINCT FROM (OLD.code,OLD.scope,OLD.policies) THEN
      RAISE EXCEPTION 'template safety bounds are immutable' USING ERRCODE='23514';
    END IF;
  ELSE
    IF (NEW.code,NEW.is_write,NEW.dual_control) IS DISTINCT FROM (OLD.code,OLD.is_write,OLD.dual_control) THEN
      RAISE EXCEPTION 'permission identity and safety class are immutable' USING ERRCODE='23514';
    END IF;
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER groups_template_safety BEFORE UPDATE ON role_templates FOR EACH ROW EXECUTE FUNCTION groups_guard_catalogue();
CREATE TRIGGER groups_permission_safety BEFORE UPDATE ON permissions FOR EACH ROW EXECUTE FUNCTION groups_guard_catalogue();
