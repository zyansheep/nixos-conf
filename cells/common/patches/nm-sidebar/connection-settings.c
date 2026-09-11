#include "sections/connection-settings.h"
#include "sections/connection_info.h"
#include "sections/helpers.h"
#include "actions/profile-model.h"
#include "data/labels.h"
#include <string.h>

typedef struct {
  GtkWidget *method, *addresses, *gateway, *dns, *automatic_dns;
  GtkWidget *fields;
  const char *const *methods;
} IpFields;

typedef struct {
  NetworkSidebarActions *actions;
  NMClient *client;
  AdwNavigationView *view;
  AdwNavigationPage *page;
  NMRemoteConnection *profile;
  NMConnection *baseline;
  guint64 version;
  GtkWidget *name, *ssid, *autoconnect, *metered, *hidden, *password;
  GtkWidget *security, *identity, *anonymous, *domain, *eap_group, *eap_method, *ca;
  GtkWidget *body, *error, *save;
  IpFields ip4, ip6;
} Editor;

typedef struct {
  NetworkSidebarActions *actions;
  NMClient *client;
  AdwNavigationView *view;
  NMRemoteConnection *profile;
  NMActiveConnection *active;
  GtkWidget *content;
} Details;

static const char *const methods4[] = { "auto", "manual", "link-local", "shared", "disabled", NULL };
static const char *const methods6[] = { "auto", "manual", "dhcp", "link-local", "shared", "disabled", "ignore", NULL };
static const char *const labels4[] = { "Automatic (DHCP)", "Manual", "Link-local", "Shared", "Disabled", NULL };
static const char *const labels6[] = { "Automatic", "Manual", "DHCP only", "Link-local", "Shared", "Disabled", "Unmanaged", NULL };

static GtkWidget *
entry(GtkWidget *group, const char *title, const char *value, gboolean secret)
{
  GtkWidget *row = secret ? adw_password_entry_row_new() : adw_entry_row_new();
  adw_preferences_row_set_title(ADW_PREFERENCES_ROW(row), title);
  gtk_editable_set_text(GTK_EDITABLE(row), value ? value : "");
  adw_preferences_group_add(ADW_PREFERENCES_GROUP(group), row);
  return row;
}

static GtkWidget *
toggle(GtkWidget *group, const char *title, gboolean active)
{
  GtkWidget *row = adw_switch_row_new();
  adw_preferences_row_set_title(ADW_PREFERENCES_ROW(row), title);
  adw_switch_row_set_active(ADW_SWITCH_ROW(row), active);
  adw_preferences_group_add(ADW_PREFERENCES_GROUP(group), row);
  return row;
}

static GtkWidget *
combo(GtkWidget *group, const char *title, const char *const *labels, guint selected)
{
  GtkWidget *row = adw_combo_row_new();
  GtkStringList *model = gtk_string_list_new(labels);
  adw_preferences_row_set_title(ADW_PREFERENCES_ROW(row), title);
  adw_combo_row_set_model(ADW_COMBO_ROW(row), G_LIST_MODEL(model));
  g_object_unref(model);
  adw_combo_row_set_selected(ADW_COMBO_ROW(row), selected);
  adw_preferences_group_add(ADW_PREFERENCES_GROUP(group), row);
  return row;
}

static void
info(GtkWidget *group, const char *title, const char *value)
{
  GtkWidget *row = network_sidebar_action_row(title, value && *value ? value : "—", NULL);
  adw_action_row_set_subtitle_selectable(ADW_ACTION_ROW(row), TRUE);
  adw_action_row_set_subtitle_lines(ADW_ACTION_ROW(row), 0);
  adw_preferences_group_add(ADW_PREFERENCES_GROUP(group), row);
}

static GtkWidget *
group(GtkWidget *content, const char *title, const char *description)
{
  GtkWidget *widget = network_sidebar_section_group(title);
  if (description) adw_preferences_group_set_description(ADW_PREFERENCES_GROUP(widget), description);
  gtk_box_append(GTK_BOX(content), widget);
  return widget;
}

static AdwNavigationPage *
page_new(const char *title, GtkWidget **content, GtkWidget **header)
{
  GtkWidget *toolbar = adw_toolbar_view_new();
  GtkWidget *scroll = gtk_scrolled_window_new();
  *header = adw_header_bar_new();
  adw_header_bar_set_show_end_title_buttons(ADW_HEADER_BAR(*header), FALSE);
  adw_toolbar_view_add_top_bar(ADW_TOOLBAR_VIEW(toolbar), *header);
  *content = gtk_box_new(GTK_ORIENTATION_VERTICAL, 20);
  gtk_widget_set_margin_start(*content, 12);
  gtk_widget_set_margin_end(*content, 12);
  gtk_widget_set_margin_top(*content, 12);
  gtk_widget_set_margin_bottom(*content, 20);
  gtk_scrolled_window_set_policy(GTK_SCROLLED_WINDOW(scroll), GTK_POLICY_NEVER, GTK_POLICY_AUTOMATIC);
  gtk_scrolled_window_set_child(GTK_SCROLLED_WINDOW(scroll), *content);
  adw_toolbar_view_set_content(ADW_TOOLBAR_VIEW(toolbar), scroll);
  return adw_navigation_page_new(toolbar, title);
}

static void
editor_free(Editor *editor)
{
  network_sidebar_actions_unref(editor->actions);
  g_clear_object(&editor->client);
  g_clear_object(&editor->profile);
  g_clear_object(&editor->baseline);
  g_free(editor);
}

static void
show_error(Editor *editor, const char *message)
{
  gtk_label_set_text(GTK_LABEL(editor->error), message ? message : "Could not save the connection.");
  gtk_widget_set_visible(editor->error, TRUE);
  gtk_widget_grab_focus(editor->error);
}

static void
ip_method_changed(GObject *object, GParamSpec *pspec, gpointer user_data)
{
  IpFields *fields = user_data;
  guint selected = adw_combo_row_get_selected(ADW_COMBO_ROW(fields->method));
  const char *method = fields->methods[selected];
  gboolean enabled = g_strcmp0(method, "disabled") && g_strcmp0(method, "ignore");
  (void) object;
  (void) pspec;
  gtk_widget_set_sensitive(fields->fields, enabled);
}

static void
ip_fields_new(GtkWidget *content, const char *title, NMSettingIPConfig *setting, int family, IpFields *fields)
{
  const char *method = setting ? nm_setting_ip_config_get_method(setting) : "auto";
  const char *const *labels = family == AF_INET ? labels4 : labels6;
  guint selected = 0;
  g_autofree char *addresses = sidebar_profile_addresses(setting);
  g_autofree char *dns = sidebar_profile_dns(setting);
  GtkWidget *settings_group = group(content, title, NULL);
  fields->methods = family == AF_INET ? methods4 : methods6;
  for (guint i = 0; fields->methods[i]; i++)
    if (g_strcmp0(method, fields->methods[i]) == 0) selected = i;
  fields->method = combo(settings_group, "Address configuration", labels, selected);
  fields->fields = group(content, NULL, "Separate multiple addresses or DNS servers with commas. Blank fields use network defaults.");
  fields->addresses = entry(fields->fields, "Addresses / prefixes", addresses, FALSE);
  fields->gateway = entry(fields->fields, "Gateway", setting ? nm_setting_ip_config_get_gateway(setting) : NULL, FALSE);
  fields->automatic_dns = toggle(fields->fields, "Use automatic DNS", !setting || !nm_setting_ip_config_get_ignore_auto_dns(setting));
  fields->dns = entry(fields->fields, "Additional DNS servers", dns, FALSE);
  g_signal_connect(fields->method, "notify::selected", G_CALLBACK(ip_method_changed), fields);
  ip_method_changed(NULL, NULL, fields);
}

static gboolean
apply_ip(Editor *editor, NMConnection *draft, int family, IpFields *fields, GError **error)
{
  (void) editor;
  return sidebar_profile_set_ip(draft, family,
    fields->methods[adw_combo_row_get_selected(ADW_COMBO_ROW(fields->method))],
    gtk_editable_get_text(GTK_EDITABLE(fields->addresses)),
    gtk_editable_get_text(GTK_EDITABLE(fields->gateway)),
    gtk_editable_get_text(GTK_EDITABLE(fields->dns)),
    adw_switch_row_get_active(ADW_SWITCH_ROW(fields->automatic_dns)), error);
}

static void
security_changed(GObject *object, GParamSpec *pspec, gpointer user_data)
{
  Editor *editor = user_data;
  guint mode = adw_combo_row_get_selected(ADW_COMBO_ROW(editor->security));
  (void) object;
  (void) pspec;
  gtk_widget_set_sensitive(editor->password, mode != 0);
  gtk_widget_set_visible(editor->eap_group, mode == 3);
}

static void
save_finished(GObject *source, GAsyncResult *result, gpointer user_data)
{
  AdwNavigationPage *page = user_data;
  Editor *editor = g_object_get_data(G_OBJECT(page), "editor");
  g_autoptr(GError) error = NULL;
  (void) source;
  gtk_widget_set_sensitive(editor->body, TRUE);
  gtk_widget_set_sensitive(editor->save, TRUE);
  adw_navigation_page_set_can_pop(page, TRUE);
  if (network_sidebar_actions_save_profile_finish(result, &error))
    adw_navigation_view_pop(editor->view);
  else
    show_error(editor, error->message);
  g_object_unref(page);
}

static void
save_clicked(GtkButton *button, gpointer user_data)
{
  Editor *editor = user_data;
  g_autoptr(NMConnection) draft = nm_simple_connection_new_clone(editor->baseline);
  g_autoptr(GError) error = NULL;
  NMSettingConnection *connection = nm_connection_get_setting_connection(draft);
  NMSettingWireless *wireless = nm_connection_get_setting_wireless(draft);
  NMSettingWirelessSecurity *security = nm_connection_get_setting_wireless_security(draft);
  NMSetting8021x *eap = nm_connection_get_setting_802_1x(draft);
  const char *name = gtk_editable_get_text(GTK_EDITABLE(editor->name));
  const char *ssid = gtk_editable_get_text(GTK_EDITABLE(editor->ssid));
  const char *password = editor->password ? gtk_editable_get_text(GTK_EDITABLE(editor->password)) : "";
  guint metered = adw_combo_row_get_selected(ADW_COMBO_ROW(editor->metered));
  (void) button;

  if (!*name || !*ssid || strlen(ssid) > 32) {
    show_error(editor, "Enter a profile name and a Wi-Fi name of 1–32 bytes.");
    return;
  }
  g_object_set(connection, NM_SETTING_CONNECTION_ID, name,
               NM_SETTING_CONNECTION_AUTOCONNECT, adw_switch_row_get_active(ADW_SWITCH_ROW(editor->autoconnect)),
               NM_SETTING_CONNECTION_METERED, metered == 0 ? NM_METERED_UNKNOWN : metered == 1 ? NM_METERED_YES : NM_METERED_NO, NULL);
  /* Preserve non-UTF-8 SSIDs unless the displayed field was actually edited. */
  g_autofree char *original_ssid = network_sidebar_ssid_text_from_bytes(nm_setting_wireless_get_ssid(wireless));
  if (g_strcmp0(ssid, original_ssid) != 0) {
    g_autoptr(GBytes) bytes = g_bytes_new(ssid, strlen(ssid));
    g_object_set(wireless, NM_SETTING_WIRELESS_SSID, bytes, NULL);
  }
  g_object_set(wireless, NM_SETTING_WIRELESS_HIDDEN, adw_switch_row_get_active(ADW_SWITCH_ROW(editor->hidden)), NULL);
  if (editor->security) {
    guint mode = adw_combo_row_get_selected(ADW_COMBO_ROW(editor->security));
    if (mode != 0) {
      NMSetting *setting = nm_setting_wireless_security_new();
      g_object_set(setting, NM_SETTING_WIRELESS_SECURITY_KEY_MGMT, mode == 3 ? "wpa-eap" : mode == 2 ? "sae" : "wpa-psk", NULL);
      nm_connection_add_setting(draft, setting);
      security = NM_SETTING_WIRELESS_SECURITY(setting);
      if (mode == 3) {
        NMSetting *auth = nm_setting_802_1x_new();
        eap = NM_SETTING_802_1X(auth);
        gboolean ttls = adw_combo_row_get_selected(ADW_COMBO_ROW(editor->eap_method)) == 1;
        const char *ca = gtk_editable_get_text(GTK_EDITABLE(editor->ca));
        const char *domain = gtk_editable_get_text(GTK_EDITABLE(editor->domain));
        if (!*domain) { g_object_unref(auth); show_error(editor, "Enter the authentication server domain supplied by your network administrator."); return; }
        nm_setting_802_1x_add_eap_method(eap, ttls ? "ttls" : "peap");
        g_object_set(eap, NM_SETTING_802_1X_PHASE2_AUTH, ttls ? "pap" : "mschapv2",
                     NM_SETTING_802_1X_SYSTEM_CA_CERTS, !*ca, NULL);
        if (*ca && !nm_setting_802_1x_set_ca_cert(eap, ca, NM_SETTING_802_1X_CK_SCHEME_PATH, NULL, &error)) {
          g_object_unref(auth); show_error(editor, error->message); return;
        }
        nm_connection_add_setting(draft, auth);
      }
      if (!*password) { show_error(editor, "Enter the Wi-Fi password."); return; }
    } else security = NULL;
  }
  if (security && *password) {
    const char *key = nm_setting_wireless_security_get_key_mgmt(security);
    if (g_strcmp0(key, "wpa-psk") == 0 || g_strcmp0(key, "sae") == 0) {
      if ((g_strcmp0(key, "wpa-psk") == 0 && !nm_utils_wpa_psk_valid(password)) ||
          (g_strcmp0(key, "sae") == 0 && strlen(password) > 63)) {
        show_error(editor, "Use a valid Wi-Fi password (WPA2: 8–63 characters or 64 hexadecimal digits).");
        return;
      }
      g_object_set(security, NM_SETTING_WIRELESS_SECURITY_PSK, password,
                   NM_SETTING_WIRELESS_SECURITY_PSK_FLAGS, NM_SETTING_SECRET_FLAG_NONE, NULL);
    }
  }
  if (eap && editor->identity) {
    const char *identity = gtk_editable_get_text(GTK_EDITABLE(editor->identity));
    const char *anonymous = gtk_editable_get_text(GTK_EDITABLE(editor->anonymous));
    const char *domain = gtk_editable_get_text(GTK_EDITABLE(editor->domain));
    g_object_set(eap, NM_SETTING_802_1X_IDENTITY, *identity ? identity : NULL,
                 NM_SETTING_802_1X_ANONYMOUS_IDENTITY, *anonymous ? anonymous : NULL,
                 NM_SETTING_802_1X_DOMAIN_SUFFIX_MATCH, *domain ? domain : NULL, NULL);
    if (*password) g_object_set(eap, NM_SETTING_802_1X_PASSWORD, password,
                               NM_SETTING_802_1X_PASSWORD_FLAGS, NM_SETTING_SECRET_FLAG_NONE, NULL);
  }
  if (!apply_ip(editor, draft, AF_INET, &editor->ip4, &error) ||
      !apply_ip(editor, draft, AF_INET6, &editor->ip6, &error) ||
      !nm_connection_verify(draft, &error)) {
    show_error(editor, error->message);
    return;
  }
  gtk_widget_set_visible(editor->error, FALSE);
  gtk_widget_set_sensitive(editor->body, FALSE);
  gtk_widget_set_sensitive(editor->save, FALSE);
  adw_navigation_page_set_can_pop(editor->page, FALSE);
  network_sidebar_actions_save_profile(editor->actions, editor->profile, draft, editor->version,
                                      save_finished, g_object_ref(editor->page));
}

void
network_sidebar_edit_profile(NetworkSidebarActions *actions, NMClient *client,
                             AdwNavigationView *view, NMRemoteConnection *profile,
                             const char *initial_ssid, gboolean enterprise)
{
  Editor *editor = g_new0(Editor, 1);
  GtkWidget *header, *settings_group;
  NMSettingConnection *connection;
  NMSettingWireless *wireless;
  NMSettingWirelessSecurity *security;
  NMSetting8021x *eap;
  g_autofree char *ssid = NULL;
  const char *const meter_labels[] = { "Automatic", "Metered", "Unmetered", NULL };
  const char *const security_labels[] = { "Open network", "WPA2 Personal", "WPA3 Personal", "Enterprise (802.1X)", NULL };
  const char *const eap_labels[] = { "PEAP (MSCHAPv2)", "TTLS (PAP)", NULL };

  editor->actions = network_sidebar_actions_ref(actions);
  editor->client = g_object_ref(client);
  editor->view = view;
  if (profile) {
    editor->profile = g_object_ref(profile);
    editor->baseline = nm_simple_connection_new_clone(NM_CONNECTION(profile));
    editor->version = nm_remote_connection_get_version_id(profile);
  } else {
    g_autofree char *uuid = g_uuid_string_random();
    NMSetting *setting = nm_setting_connection_new();
    editor->baseline = nm_simple_connection_new();
    g_object_set(setting, NM_SETTING_CONNECTION_UUID, uuid, NM_SETTING_CONNECTION_ID, initial_ssid ? initial_ssid : "New Wi-Fi",
                 NM_SETTING_CONNECTION_TYPE, NM_SETTING_WIRELESS_SETTING_NAME, NM_SETTING_CONNECTION_AUTOCONNECT, FALSE, NULL);
    nm_connection_add_setting(editor->baseline, setting);
    setting = nm_setting_wireless_new();
    g_autoptr(GBytes) bytes = g_bytes_new(initial_ssid ? initial_ssid : "", initial_ssid ? strlen(initial_ssid) : 0);
    g_object_set(setting, NM_SETTING_WIRELESS_MODE, "infrastructure", NM_SETTING_WIRELESS_SSID, bytes, NULL);
    nm_connection_add_setting(editor->baseline, setting);
  }
  connection = nm_connection_get_setting_connection(editor->baseline);
  wireless = nm_connection_get_setting_wireless(editor->baseline);
  security = nm_connection_get_setting_wireless_security(editor->baseline);
  eap = nm_connection_get_setting_802_1x(editor->baseline);
  if (!wireless) { editor_free(editor); return; }
  ssid = profile ? network_sidebar_ssid_text_from_bytes(nm_setting_wireless_get_ssid(wireless)) : g_strdup(initial_ssid ? initial_ssid : "");
  editor->page = page_new(profile ? "Edit Wi-Fi" : "Add Wi-Fi", &editor->body, &header);
  g_object_set_data_full(G_OBJECT(editor->page), "editor", editor, (GDestroyNotify) editor_free);
  editor->save = gtk_button_new_with_label("Save");
  gtk_widget_add_css_class(editor->save, "suggested-action");
  g_signal_connect(editor->save, "clicked", G_CALLBACK(save_clicked), editor);
  adw_header_bar_pack_end(ADW_HEADER_BAR(header), editor->save);
  editor->error = gtk_label_new("");
  gtk_label_set_wrap(GTK_LABEL(editor->error), TRUE);
  gtk_widget_set_focusable(editor->error, TRUE);
  gtk_widget_add_css_class(editor->error, "error");
  gtk_widget_set_visible(editor->error, FALSE);
  gtk_box_append(GTK_BOX(editor->body), editor->error);
  settings_group = group(editor->body, "Connection", "Save keeps these settings for your next connection. Go back to discard changes.");
  editor->name = entry(settings_group, "Profile name", nm_setting_connection_get_id(connection), FALSE);
  editor->autoconnect = toggle(settings_group, "Connect automatically", nm_setting_connection_get_autoconnect(connection));
  NMMetered metered = nm_setting_connection_get_metered(connection);
  editor->metered = combo(settings_group, "Data usage", meter_labels, metered == NM_METERED_YES ? 1 : metered == NM_METERED_NO ? 2 : 0);
  settings_group = group(editor->body, "Wi-Fi", profile ? "Leave the password blank to keep the saved password." : NULL);
  editor->ssid = entry(settings_group, "Network name (SSID)", ssid, FALSE);
  editor->hidden = toggle(settings_group, "Hidden network", nm_setting_wireless_get_hidden(wireless));
  if (!profile) editor->security = combo(settings_group, "Security", security_labels, enterprise ? 3 : 1);
  else info(settings_group, "Security", security ? nm_setting_wireless_security_get_key_mgmt(security) : "Open");
  if (!profile || (security && (g_strcmp0(nm_setting_wireless_security_get_key_mgmt(security), "wpa-psk") == 0 ||
                               g_strcmp0(nm_setting_wireless_security_get_key_mgmt(security), "sae") == 0)) || eap)
    editor->password = entry(settings_group, profile ? "New password (optional)" : "Password", NULL, TRUE);
  if (eap || !profile) {
    settings_group = group(editor->body, "Enterprise authentication", eap ? "Existing EAP methods and certificate settings are retained." : "Use the server domain and certificate information supplied by your network administrator.");
    editor->eap_group = settings_group;
    if (!profile) editor->eap_method = combo(settings_group, "Authentication", eap_labels, 0);
    editor->identity = entry(settings_group, "Identity", eap ? nm_setting_802_1x_get_identity(eap) : NULL, FALSE);
    editor->anonymous = entry(settings_group, "Anonymous identity", eap ? nm_setting_802_1x_get_anonymous_identity(eap) : NULL, FALSE);
    editor->domain = entry(settings_group, "Server domain suffix", eap ? nm_setting_802_1x_get_domain_suffix_match(eap) : NULL, FALSE);
    if (!profile) {
      editor->ca = entry(settings_group, "CA certificate path (optional)", NULL, FALSE);
      g_signal_connect(editor->security, "notify::selected", G_CALLBACK(security_changed), editor);
      security_changed(NULL, NULL, editor);
    }
  }
  ip_fields_new(editor->body, "IPv4", nm_connection_get_setting_ip4_config(editor->baseline), AF_INET, &editor->ip4);
  ip_fields_new(editor->body, "IPv6", nm_connection_get_setting_ip6_config(editor->baseline), AF_INET6, &editor->ip6);
  adw_navigation_view_push(view, editor->page);
}

static void
details_free(Details *details)
{
  network_sidebar_actions_unref(details->actions);
  g_clear_object(&details->client);
  g_clear_object(&details->profile);
  g_clear_object(&details->active);
  g_free(details);
}

static void
details_edit(GtkButton *button, gpointer user_data)
{
  Details *details = user_data;
  (void) button;
  network_sidebar_actions_edit_connection(details->actions, details->profile);
}

static void
details_disconnect(GtkButton *button, gpointer user_data)
{
  Details *details = user_data;
  (void) button;
  if (details->active && nm_active_connection_get_state(details->active) < NM_ACTIVE_CONNECTION_STATE_DEACTIVATING)
    network_sidebar_actions_deactivate(details->actions, details->active);
  else if (details->profile)
    network_sidebar_actions_activate_saved_wifi_profile(details->actions, details->profile);
}

static void
forget_confirmed(GtkButton *button, gpointer user_data)
{
  Details *details = user_data;
  (void) button;
  network_sidebar_actions_delete_connection(details->actions, details->profile);
  adw_navigation_view_pop(details->view);
  adw_navigation_view_pop(details->view);
}

static void
details_forget(GtkButton *button, gpointer user_data)
{
  Details *details = user_data;
  GtkWidget *content, *header;
  AdwNavigationPage *page = page_new("Forget network?", &content, &header);
  GtkWidget *notice = gtk_label_new("This removes the saved Wi-Fi profile and password. If connected, you will be disconnected.");
  GtkWidget *confirm = gtk_button_new_with_label("Forget network");
  (void) button;
  gtk_label_set_wrap(GTK_LABEL(notice), TRUE);
  gtk_widget_add_css_class(confirm, "destructive-action");
  gtk_box_append(GTK_BOX(content), notice);
  gtk_box_append(GTK_BOX(content), confirm);
  /* The underlying details page remains on the navigation stack. */
  g_signal_connect(confirm, "clicked", G_CALLBACK(forget_confirmed), details);
  adw_navigation_view_push(details->view, page);
}

static void
details_refresh(AdwNavigationPage *page, gpointer user_data)
{
  Details *details = user_data;
  GtkWidget *settings_group, *buttons, *button;
  const GPtrArray *active = nm_client_get_active_connections(details->client);
  (void) page;
  g_clear_object(&details->active);
  for (guint i = 0; active && i < active->len; i++) {
    NMActiveConnection *item = g_ptr_array_index(active, i);
    if (details->profile && g_strcmp0(nm_active_connection_get_uuid(item), nm_connection_get_uuid(NM_CONNECTION(details->profile))) == 0)
      details->active = g_object_ref(item);
  }
  network_sidebar_clear_box(GTK_BOX(details->content));
  buttons = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
  button = gtk_button_new_with_label(details->active ? "Disconnect" : "Connect");
  gtk_widget_set_hexpand(button, TRUE);
  g_signal_connect(button, "clicked", G_CALLBACK(details_disconnect), details);
  gtk_box_append(GTK_BOX(buttons), button);
  if (details->profile) {
    button = gtk_button_new_with_label("Forget…");
    gtk_widget_add_css_class(button, "destructive-action");
    g_signal_connect(button, "clicked", G_CALLBACK(details_forget), details);
    gtk_box_append(GTK_BOX(buttons), button);
  }
  gtk_box_append(GTK_BOX(details->content), buttons);
  if (details->active)
    network_sidebar_add_active_connection_info_content(GTK_BOX(details->content), details->client, details->active);
  if (details->profile) {
    NMConnection *connection = NM_CONNECTION(details->profile);
    NMSettingConnection *general = nm_connection_get_setting_connection(connection);
    NMSettingWireless *wifi = nm_connection_get_setting_wireless(connection);
    NMSettingWirelessSecurity *security = nm_connection_get_setting_wireless_security(connection);
    settings_group = group(details->content, "Saved configuration", NULL);
    info(settings_group, "Profile", nm_connection_get_id(connection));
    info(settings_group, "Connect automatically", nm_setting_connection_get_autoconnect(general) ? "Yes" : "No");
    if (wifi) {
      g_autofree char *ssid = network_sidebar_ssid_text_from_bytes(nm_setting_wireless_get_ssid(wifi));
      info(settings_group, "SSID", ssid);
      info(settings_group, "Security", security ? nm_setting_wireless_security_get_key_mgmt(security) : "Open");
      info(settings_group, "Hidden network", nm_setting_wireless_get_hidden(wifi) ? "Yes" : "No");
      info(settings_group, "MAC address policy", nm_setting_wireless_get_cloned_mac_address(wifi));
    }
    for (int family = 0; family < 2; family++) {
      NMSettingIPConfig *ip = family ? nm_connection_get_setting_ip6_config(connection) : nm_connection_get_setting_ip4_config(connection);
      if (!ip) continue;
      g_autofree char *addresses = sidebar_profile_addresses(ip);
      g_autofree char *dns = sidebar_profile_dns(ip);
      settings_group = group(details->content, family ? "Saved IPv6 settings" : "Saved IPv4 settings", NULL);
      info(settings_group, "Address configuration", nm_setting_ip_config_get_method(ip));
      info(settings_group, "Addresses", addresses);
      info(settings_group, "Gateway", nm_setting_ip_config_get_gateway(ip));
      info(settings_group, "Automatic DNS", nm_setting_ip_config_get_ignore_auto_dns(ip) ? "No" : "Yes");
      info(settings_group, "DNS servers", dns);
    }
  }
}

static void
details_nm_changed(GObject *source, GParamSpec *pspec, gpointer user_data)
{
  AdwNavigationPage *page = user_data;
  Details *details = g_object_get_data(G_OBJECT(page), "details");
  (void) source;
  (void) pspec;
  if (adw_navigation_view_get_visible_page(details->view) == page)
    details_refresh(page, details);
}

static void
details_profile_changed(NMConnection *profile, gpointer user_data)
{
  (void) profile;
  details_nm_changed(NULL, NULL, user_data);
}

void
network_sidebar_show_profile(NetworkSidebarActions *actions, NMClient *client,
                             AdwNavigationView *view, NMRemoteConnection *profile,
                             NMActiveConnection *active)
{
  Details *details = g_new0(Details, 1);
  GtkWidget *header;
  g_autofree char *title = profile ? network_sidebar_connection_name(NM_CONNECTION(profile), "Wi-Fi") :
                                  network_sidebar_active_connection_name(active, "Wi-Fi");
  AdwNavigationPage *page = page_new(title, &details->content, &header);
  details->actions = network_sidebar_actions_ref(actions);
  details->client = g_object_ref(client);
  details->view = view;
  details->profile = profile ? g_object_ref(profile) : NULL;
  details->active = active ? g_object_ref(active) : NULL;
  g_object_set_data_full(G_OBJECT(page), "details", details, (GDestroyNotify) details_free);
  if (profile) {
    GtkWidget *edit = gtk_button_new_with_label("Edit");
    g_signal_connect(edit, "clicked", G_CALLBACK(details_edit), details);
    adw_header_bar_pack_end(ADW_HEADER_BAR(header), edit);
  }
  if (profile) g_signal_connect_object(profile, "changed", G_CALLBACK(details_profile_changed), page, 0);
  g_signal_connect(page, "shown", G_CALLBACK(details_refresh), details);
  g_signal_connect_object(client, "notify::active-connections", G_CALLBACK(details_nm_changed), page, 0);
  adw_navigation_view_push(view, page);
}
