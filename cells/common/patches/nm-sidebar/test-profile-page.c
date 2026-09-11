/* Exercise the actual form and async save path against a disposable profile.
 * Include the UI implementation to inspect its private draft state without
 * adding a testing API to the installed application. No window is presented. */
#include "sections/connection-settings.c"
#include <stdio.h>

static NMRemoteConnection *remote;
static GError *failure;
static gboolean adding;
static void added(GObject *source, GAsyncResult *result, gpointer data)
{
  (void) data;
  remote = nm_client_add_connection_finish(NM_CLIENT(source), result, &failure);
  adding = FALSE;
}
static AdwToast *toast(const char *message, gpointer data) { (void) message; (void) data; return NULL; }
static void refresh(guint delay, gpointer data) { (void) delay; (void) data; }
static void settle(void)
{
  gint64 until = g_get_monotonic_time() + 300000;
  while (g_get_monotonic_time() < until) { while (g_main_context_iteration(NULL, FALSE)); g_usleep(1000); }
}
static gboolean wait_save(Editor *editor)
{
  gint64 until = g_get_monotonic_time() + 10000000;
  while (editor->saving && g_get_monotonic_time() < until) g_main_context_iteration(NULL, TRUE);
  settle();
  if (gtk_widget_get_visible(editor->error)) fprintf(stderr, "Form error: %s\n", gtk_label_get_text(GTK_LABEL(editor->error)));
  return !editor->saving && !gtk_widget_get_visible(editor->error);
}
#define CHECK(test, message) do { if (!(test)) { fprintf(stderr, "FAIL: %s\n", message); goto cleanup; } } while (0)

int main(int argc, char **argv)
{
  (void) argv;
  gboolean inherited_mac = argc > 1;
  int status = 1;
  adw_init();
  g_autoptr(NMClient) client = nm_client_new(NULL, &failure);
  if (!client) { fprintf(stderr, "%s\n", failure->message); return 1; }
  g_autoptr(NetworkSidebarActions) actions = network_sidebar_actions_new(client, toast, refresh, NULL);
  g_autoptr(NMConnection) connection = nm_simple_connection_new();
  g_autofree char *uuid = g_uuid_string_random();
  g_autofree char *name = g_strdup_printf("nm-sidebar-test-%.8s", uuid);
  NMSetting *setting = nm_setting_connection_new();
  g_object_set(setting, NM_SETTING_CONNECTION_ID, name, NM_SETTING_CONNECTION_UUID, uuid,
    NM_SETTING_CONNECTION_TYPE, NM_SETTING_WIRELESS_SETTING_NAME, NM_SETTING_CONNECTION_AUTOCONNECT, FALSE, NULL);
  nm_connection_add_setting(connection, setting);
  setting = nm_setting_wireless_new();
  g_autoptr(GBytes) ssid = g_bytes_new(name, strlen(name));
  g_object_set(setting, NM_SETTING_WIRELESS_SSID, ssid, NM_SETTING_WIRELESS_MODE, "infrastructure",
               NM_SETTING_WIRELESS_CLONED_MAC_ADDRESS, inherited_mac ? NULL : "02:00:00:00:00:01", NULL);
  nm_connection_add_setting(connection, setting);
  setting = nm_setting_wireless_security_new();
  g_object_set(setting, NM_SETTING_WIRELESS_SECURITY_KEY_MGMT, "wpa-psk", NM_SETTING_WIRELESS_SECURITY_PSK, "test-original-password", NULL);
  nm_connection_add_setting(connection, setting);
  setting = nm_setting_ip4_config_new(); g_object_set(setting, NM_SETTING_IP_CONFIG_METHOD, "auto", NULL); nm_connection_add_setting(connection, setting);
  setting = nm_setting_ip6_config_new(); g_object_set(setting, NM_SETTING_IP_CONFIG_METHOD, "auto", NULL); nm_connection_add_setting(connection, setting);
  GtkWidget *view_widget = g_object_ref_sink(adw_navigation_view_new());
  AdwNavigationView *view = ADW_NAVIGATION_VIEW(view_widget);
  adw_navigation_view_add(view, adw_navigation_page_new(gtk_box_new(GTK_ORIENTATION_VERTICAL, 0), "Networks"));
  adding = TRUE;
  nm_client_add_connection_async(client, connection, FALSE, NULL, added, NULL);
  while (adding) g_main_context_iteration(NULL, TRUE);
  settle();
  CHECK(remote, "create disposable disconnected profile");
  network_sidebar_edit_profile(actions, client, view, remote, NULL, FALSE);
  AdwNavigationPage *page = adw_navigation_view_get_visible_page(view);
  Editor *editor = g_object_get_data(G_OBJECT(page), "editor");
  CHECK(editor && !editor->dirty && !gtk_widget_get_visible(editor->footer), "initial form has no Save bar");
  GListModel *mac_options = gtk_drop_down_get_model(GTK_DROP_DOWN(editor->mac));
  for (guint i = 0; i < g_list_model_get_n_items(mac_options); i++) {
    g_autoptr(GtkStringObject) option = g_list_model_get_item(mac_options, i);
    CHECK(g_strcmp0(gtk_string_object_get_string(option), "Default"), "MAC choices only show policy names");
  }
  if (inherited_mac) {
    GtkStringObject *selected = gtk_drop_down_get_selected_item(GTK_DROP_DOWN(editor->mac));
    CHECK(g_strcmp0(gtk_string_object_get_string(selected), "Stable per network") == 0, "inherited MAC policy resolves to stable per network");
  }
  gtk_switch_set_active(GTK_SWITCH(editor->autoconnect), TRUE);
  CHECK(editor->dirty && gtk_widget_get_visible(editor->footer), "editing reveals Save bar");
  CHECK(!nm_setting_connection_get_autoconnect(nm_connection_get_setting_connection(NM_CONNECTION(remote))), "draft does not change NetworkManager");
  Field *auto_field = g_ptr_array_index(editor->fields, 0);
  CHECK(gtk_widget_get_sensitive(auto_field->undo), "edited field offers undo");
  field_revert(NULL, auto_field);
  CHECK(!editor->dirty && !gtk_widget_get_visible(editor->footer), "field revert clears draft and Save bar");
  gtk_editable_set_text(GTK_EDITABLE(editor->name), "");
  save_clicked(NULL, editor);
  CHECK(gtk_widget_get_visible(editor->error) && editor->dirty, "invalid name stays editable");
  revert_all(NULL, editor);
  CHECK(!editor->dirty && !gtk_widget_get_visible(editor->error), "revert all clears changes and error");
  gtk_drop_down_set_selected(GTK_DROP_DOWN(editor->ip4.method), 1);
  gtk_editable_set_text(GTK_EDITABLE(editor->ip4.addresses), "invalid-address");
  save_clicked(NULL, editor);
  CHECK(gtk_widget_get_visible(editor->error) && !editor->saving, "invalid manual IP cannot save");
  revert_all(NULL, editor);
  puts("PASS: conditional Save bar, draft isolation, field undo, revert all and validation");

  gtk_editable_set_text(GTK_EDITABLE(editor->ip4.dns), "192.0.2.53");
  save_clicked(NULL, editor);
  CHECK(wait_save(editor), "save DNS override");
  CHECK(adw_navigation_view_get_visible_page(view) == page && !editor->dirty && !gtk_widget_get_visible(editor->footer), "save stays on unified page and resets baseline");
  CHECK(nm_setting_ip_config_get_num_dns(nm_connection_get_setting_ip4_config(NM_CONNECTION(remote))) == 1, "saved DNS reaches NetworkManager");
  if (inherited_mac)
    CHECK(!nm_setting_wireless_get_cloned_mac_address(nm_connection_get_setting_wireless(NM_CONNECTION(remote))), "unrelated save preserves inherited MAC policy");
  gtk_drop_down_set_selected(GTK_DROP_DOWN(editor->metered), 1);
  save_clicked(NULL, editor);
  CHECK(wait_save(editor), "second save uses current profile version");
  CHECK(nm_setting_connection_get_metered(nm_connection_get_setting_connection(NM_CONNECTION(remote))) == NM_METERED_YES, "second save persists new field");
  CHECK(!editor->dirty && !gtk_widget_get_visible(editor->footer), "second save clears dirty controls");
  puts("PASS: repeated saves persist edits without leaving the details page");

  gtk_editable_set_text(GTK_EDITABLE(editor->password), "test-replacement-password");
  save_clicked(NULL, editor);
  CHECK(wait_save(editor), "save replacement password");
  CHECK(!*gtk_editable_get_text(GTK_EDITABLE(editor->password)) && !editor->dirty, "saved password input resets to unchanged");
  gtk_drop_down_set_selected(GTK_DROP_DOWN(editor->mac), 1);
  save_clicked(NULL, editor);
  CHECK(wait_save(editor), "save device MAC policy");
  CHECK(g_strcmp0(nm_setting_wireless_get_cloned_mac_address(nm_connection_get_setting_wireless(NM_CONNECTION(remote))), USES_IWD ? editor->device_mac : "permanent") == 0,
        "device MAC policy persisted");
  gtk_drop_down_set_selected(GTK_DROP_DOWN(editor->mac), inherited_mac ? 0 : G_N_ELEMENTS(mac_values));
  save_clicked(NULL, editor);
  CHECK(wait_save(editor), "restore desired MAC option after another policy was saved");
  CHECK(g_strcmp0(nm_setting_wireless_get_cloned_mac_address(nm_connection_get_setting_wireless(NM_CONNECTION(remote))), inherited_mac ? "stable-ssid" : "02:00:00:00:00:01") == 0,
        "selected stable or custom MAC policy is preserved");
  gtk_drop_down_set_selected(GTK_DROP_DOWN(editor->ip4.method), 4);
  save_clicked(NULL, editor);
  CHECK(wait_save(editor), "disable IPv4 on disposable profile");
  CHECK(!*gtk_editable_get_text(GTK_EDITABLE(editor->ip4.dns)) && !editor->dirty, "cleared overrides match stored settings after disabling IP");
  gtk_drop_down_set_selected(GTK_DROP_DOWN(editor->ip4.method), 0);
  CHECK(!*gtk_editable_get_text(GTK_EDITABLE(editor->ip4.dns)), "old DNS override does not silently return");
  revert_all(NULL, editor);
  CHECK(!nm_setting_connection_get_autoconnect(nm_connection_get_setting_connection(NM_CONNECTION(remote))), "test profile remains autoconnect-disabled");
  const GPtrArray *active = nm_client_get_active_connections(client);
  for (guint i = 0; active && i < active->len; i++)
    CHECK(g_strcmp0(nm_active_connection_get_uuid(g_ptr_array_index(active, i)), uuid), "test profile was never activated");
  puts("PASS: secret reset, custom MAC preservation and disabled-IP normalization; test profile stayed disconnected");
  status = 0;
cleanup:
  g_object_unref(view_widget);
  if (remote) {
    GError *error = NULL;
    if (!nm_remote_connection_delete(remote, NULL, &error)) {
      fprintf(stderr, "Could not remove %s: %s\n", name, error->message); g_clear_error(&error); status = 1;
    } else puts("PASS: temporary form-test profile removed");
    g_object_unref(remote);
  }
  g_clear_error(&failure);
  return status;
}
