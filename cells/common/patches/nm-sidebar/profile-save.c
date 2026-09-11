#include "actions/network_actions.h"

typedef struct {
  NetworkSidebarActions *actions;
  NMRemoteConnection *remote;
  NMConnection *draft;
  NMConnection *baseline;
  GVariant *new_secrets;
  guint secret_index;
  guint64 version;
} SaveProfile;

static const char *const secret_settings[] = {
  NM_SETTING_WIRELESS_SECURITY_SETTING_NAME, NM_SETTING_802_1X_SETTING_NAME, NULL
};

static void save_next(GTask *task);

static void
save_profile_free(SaveProfile *save)
{
  network_sidebar_actions_unref(save->actions);
  g_clear_object(&save->remote);
  g_clear_object(&save->draft);
  g_clear_object(&save->baseline);
  g_clear_pointer(&save->new_secrets, g_variant_unref);
  g_free(save);
}

static void
update_done(GObject *source, GAsyncResult *result, gpointer user_data)
{
  GTask *task = user_data;
  SaveProfile *save = g_task_get_task_data(task);
  g_autoptr(GError) error = NULL;
  g_autoptr(GVariant) reply = nm_remote_connection_update2_finish(NM_REMOTE_CONNECTION(source), result, &error);
  if (!reply) g_task_return_error(task, g_steal_pointer(&error));
  else {
    network_sidebar_actions_notify(save->actions, "Settings saved. Reconnect to apply them.");
    g_task_return_boolean(task, TRUE);
  }
  g_object_unref(task);
}

static void
add_done(GObject *source, GAsyncResult *result, gpointer user_data)
{
  GTask *task = user_data;
  SaveProfile *save = g_task_get_task_data(task);
  g_autoptr(GError) error = NULL;
  g_autoptr(NMRemoteConnection) remote = nm_client_add_connection_finish(NM_CLIENT(source), result, &error);
  if (!remote) g_task_return_error(task, g_steal_pointer(&error));
  else {
    network_sidebar_actions_notify(save->actions, "Wi-Fi profile saved. Select it to connect.");
    network_sidebar_actions_schedule_refresh(save->actions, 1);
    g_task_return_boolean(task, TRUE);
  }
  g_object_unref(task);
}

static void
secrets_done(GObject *source, GAsyncResult *result, gpointer user_data)
{
  GTask *task = user_data;
  SaveProfile *save = g_task_get_task_data(task);
  const char *setting = secret_settings[save->secret_index - 1];
  g_autoptr(GError) error = NULL;
  g_autoptr(GVariant) secrets = nm_remote_connection_get_secrets_finish(NM_REMOTE_CONNECTION(source), result, &error);
  if (!secrets || !nm_connection_update_secrets(save->draft, setting, secrets, &error)) {
    g_prefix_error(&error, "Could not preserve saved credentials: ");
    g_task_return_error(task, g_steal_pointer(&error));
    g_object_unref(task);
    return;
  }
  /* Explicitly entered replacements override saved secrets; all other secrets
   * remain intact, including enterprise certificate/private-key passwords. */
  g_autoptr(GVariant) replacements = g_variant_lookup_value(save->new_secrets, setting, G_VARIANT_TYPE("a{sv}"));
  if (replacements && !nm_connection_update_secrets(save->draft, setting, save->new_secrets, &error)) {
    g_task_return_error(task, g_steal_pointer(&error));
    g_object_unref(task);
    return;
  }
  save_next(task);
}

static void
save_next(GTask *task)
{
  SaveProfile *save = g_task_get_task_data(task);
  while (secret_settings[save->secret_index]) {
    const char *setting = secret_settings[save->secret_index++];
    if (g_str_equal(setting, NM_SETTING_WIRELESS_SECURITY_SETTING_NAME)) {
      NMSettingWirelessSecurity *security = nm_connection_get_setting_wireless_security(NM_CONNECTION(save->remote));
      const char *key = security ? nm_setting_wireless_security_get_key_mgmt(security) : NULL;
      /* Enterprise/OWE have no PSK here. Asking for one can needlessly request
       * a secret agent, even when the 802.1X password is stored by NM. */
      if (g_strcmp0(key, "wpa-eap") == 0 || g_strcmp0(key, "wpa-eap-suite-b-192") == 0 || g_strcmp0(key, "owe") == 0)
        continue;
    }
    if (nm_connection_get_setting_by_name(NM_CONNECTION(save->remote), setting)) {
      nm_remote_connection_get_secrets_async(save->remote, setting, NULL, secrets_done, task);
      return;
    }
  }
  /* iwd can update the profile version while fulfilling GetSecrets. Accept
   * that only when its non-secret settings still match our initial snapshot. */
  if (!nm_connection_compare(NM_CONNECTION(save->remote), save->baseline, NM_SETTING_COMPARE_FLAG_IGNORE_SECRETS)) {
    g_task_return_new_error(task, G_IO_ERROR, G_IO_ERROR_BUSY,
                           "This profile changed while saving. Reopen the editor and try again.");
    g_object_unref(task);
    return;
  }
  g_autoptr(GVariant) settings = g_variant_ref_sink(nm_connection_to_dbus(save->draft, NM_CONNECTION_SERIALIZE_ALL));
  GVariantBuilder args;
  g_variant_builder_init(&args, G_VARIANT_TYPE_VARDICT);
  /* Reject concurrent changes instead of overwriting a newer profile. */
  g_variant_builder_add(&args, "{sv}", "version-id", g_variant_new_uint64(nm_remote_connection_get_version_id(save->remote)));
  g_autoptr(GVariant) options = g_variant_ref_sink(g_variant_builder_end(&args));
  nm_remote_connection_update2(save->remote, settings,
    NM_SETTINGS_UPDATE2_FLAG_TO_DISK | NM_SETTINGS_UPDATE2_FLAG_NO_REAPPLY,
    options, NULL, update_done, task);
}

void
network_sidebar_actions_save_profile(NetworkSidebarActions *actions, NMRemoteConnection *remote,
                                    NMConnection *draft, guint64 version,
                                    GAsyncReadyCallback callback, gpointer user_data)
{
  SaveProfile *save = g_new0(SaveProfile, 1);
  GTask *task = g_task_new(NULL, NULL, callback, user_data);
  save->actions = network_sidebar_actions_ref(actions);
  save->remote = remote ? g_object_ref(remote) : NULL;
  save->draft = nm_simple_connection_new_clone(draft);
  save->baseline = remote ? nm_simple_connection_new_clone(NM_CONNECTION(remote)) : NULL;
  save->version = version;
  save->new_secrets = g_variant_ref_sink(nm_connection_to_dbus(draft, NM_CONNECTION_SERIALIZE_ONLY_SECRETS));
  g_task_set_task_data(task, save, (GDestroyNotify) save_profile_free);
  if (remote && version != nm_remote_connection_get_version_id(remote)) {
    g_task_return_new_error(task, G_IO_ERROR, G_IO_ERROR_BUSY,
                           "This profile changed while you were editing. Reopen the editor and try again.");
    g_object_unref(task);
  } else if (remote) save_next(task);
  else nm_client_add_connection_async(network_sidebar_actions_get_client(actions), save->draft, TRUE, NULL, add_done, task);
}

gboolean
network_sidebar_actions_save_profile_finish(GAsyncResult *result, GError **error)
{
  return g_task_propagate_boolean(G_TASK(result), error);
}
