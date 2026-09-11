#include "actions/network_actions.h"
#include <stdio.h>
#include <string.h>

static GMainLoop *loop;
static NMRemoteConnection *remote;
static GError *failure;
static gboolean saved;
static gboolean enterprise;
static void added(GObject *source, GAsyncResult *result, gpointer data) {
  (void)data;
  remote = nm_client_add_connection_finish(NM_CLIENT(source), result, &failure);
  g_main_loop_quit(loop);
}
static void updated(GObject *source, GAsyncResult *result, gpointer data) {
  (void)source; (void)data;
  saved = network_sidebar_actions_save_profile_finish(result, &failure);
  g_main_loop_quit(loop);
}
static AdwToast *notify(const char *message, gpointer data) {
  (void)message; (void)data; return NULL;
}
static void refresh(guint delay, gpointer data) { (void)delay; (void)data; }
static gboolean check_secret(const char *expected) {
  const char *setting_name = enterprise ? NM_SETTING_802_1X_SETTING_NAME : NM_SETTING_WIRELESS_SECURITY_SETTING_NAME;
  GVariant *secret = nm_remote_connection_get_secrets(remote, setting_name, NULL, &failure);
  if(!secret) return FALSE;
  GVariant *wireless = g_variant_lookup_value(secret, setting_name, G_VARIANT_TYPE_VARDICT);
  const char *psk = NULL;
  gboolean ok = wireless && g_variant_lookup(wireless, enterprise ? "password" : "psk", "&s", &psk) && g_strcmp0(psk, expected) == 0;
  if(wireless) g_variant_unref(wireless);
  g_variant_unref(secret);
  return ok;
}
static void settle_cache(void) {
  gint64 until=g_get_monotonic_time()+300000;
  while(g_get_monotonic_time()<until) { while(g_main_context_iteration(NULL,FALSE)); g_usleep(1000); }
}
int main(int argc, char **argv) {
  (void)argv;
  enterprise = argc > 1;
  int status = 1;
  loop = g_main_loop_new(NULL,FALSE);
  NMClient *client = nm_client_new(NULL,&failure);
  if(!client) goto done;
  NetworkSidebarActions *actions = network_sidebar_actions_new(client,notify,refresh,NULL);
  NMConnection *connection = nm_simple_connection_new();
  char *uuid = g_uuid_string_random();
  char *name = g_strdup_printf("nm-sidebar-test-%.8s",uuid);
  NMSetting *setting=nm_setting_connection_new();
  g_object_set(setting,NM_SETTING_CONNECTION_ID,name,NM_SETTING_CONNECTION_UUID,uuid,
    NM_SETTING_CONNECTION_TYPE,NM_SETTING_WIRELESS_SETTING_NAME,NM_SETTING_CONNECTION_AUTOCONNECT,FALSE,NULL);
  nm_connection_add_setting(connection,setting);
  setting=nm_setting_wireless_new();
  GBytes *ssid=g_bytes_new(name,strlen(name));
  g_object_set(setting,NM_SETTING_WIRELESS_SSID,ssid,NM_SETTING_WIRELESS_MODE,"infrastructure",NULL);
  g_bytes_unref(ssid);nm_connection_add_setting(connection,setting);
  setting=nm_setting_wireless_security_new();
  g_object_set(setting,NM_SETTING_WIRELESS_SECURITY_KEY_MGMT,"wpa-psk",NM_SETTING_WIRELESS_SECURITY_PSK,"test-original-password",NULL);
  nm_connection_add_setting(connection,setting);
  if (enterprise) {
    g_object_set(nm_connection_get_setting_wireless_security(connection),NM_SETTING_WIRELESS_SECURITY_KEY_MGMT,"wpa-eap",NM_SETTING_WIRELESS_SECURITY_PSK,NULL,NULL);
    setting=nm_setting_802_1x_new();
    nm_setting_802_1x_add_eap_method(NM_SETTING_802_1X(setting),"peap");
    g_object_set(setting,NM_SETTING_802_1X_PHASE2_AUTH,"mschapv2",NM_SETTING_802_1X_IDENTITY,"test-user",
      NM_SETTING_802_1X_DOMAIN_SUFFIX_MATCH,"example.invalid",NM_SETTING_802_1X_SYSTEM_CA_CERTS,TRUE,
      NM_SETTING_802_1X_PASSWORD,"test-original-password",NULL);
    nm_connection_add_setting(connection,setting);
  }
  setting=nm_setting_ip4_config_new();g_object_set(setting,NM_SETTING_IP_CONFIG_METHOD,"auto",NULL);nm_connection_add_setting(connection,setting);
  setting=nm_setting_ip6_config_new();g_object_set(setting,NM_SETTING_IP_CONFIG_METHOD,"auto",NULL);nm_connection_add_setting(connection,setting);
  nm_client_add_connection_async(client,connection,FALSE,NULL,added,NULL);
  g_main_loop_run(loop);
  settle_cache();
  if(!remote) goto cleanup;
  gint64 settle=g_get_monotonic_time()+500000;
  while(g_get_monotonic_time()<settle) { while(g_main_context_iteration(NULL,FALSE)); g_usleep(1000); }
  guint64 version=nm_remote_connection_get_version_id(remote);
  NMConnection *draft=nm_simple_connection_new_clone(NM_CONNECTION(remote));
  NMSettingIPConfig *ip=nm_connection_get_setting_ip4_config(draft);
  nm_setting_ip_config_add_dns(ip,"192.0.2.53");
  network_sidebar_actions_save_profile(actions,remote,draft,version,updated,NULL);
  g_main_loop_run(loop);
  settle_cache();
  g_object_unref(draft);
  if(!saved || !check_secret("test-original-password")) goto cleanup;
  if(nm_remote_connection_get_unsaved(remote) || !nm_remote_connection_get_filename(remote)) goto cleanup;
  if(nm_setting_ip_config_get_num_dns(nm_connection_get_setting_ip4_config(NM_CONNECTION(remote)))!=1) goto cleanup;
  puts("PASS: saves to persistent storage and preserves an unchanged password");
  draft=nm_simple_connection_new_clone(NM_CONNECTION(remote));
  if(enterprise) g_object_set(nm_connection_get_setting_802_1x(draft),NM_SETTING_802_1X_PASSWORD,"test-replacement-password",NULL);
  else g_object_set(nm_connection_get_setting_wireless_security(draft),NM_SETTING_WIRELESS_SECURITY_PSK,"test-replacement-password",NULL);
  network_sidebar_actions_save_profile(actions,remote,draft,nm_remote_connection_get_version_id(remote),updated,NULL);
  g_main_loop_run(loop);
  settle_cache();
  g_object_unref(draft);
  if(!saved || !check_secret("test-replacement-password")) goto cleanup;
  puts("PASS: explicitly entered password replaces the saved password");
  draft=nm_simple_connection_new_clone(NM_CONNECTION(remote));
  network_sidebar_actions_save_profile(actions,remote,draft,version,updated,NULL);
  g_main_loop_run(loop);
  settle_cache();
  g_object_unref(draft);
  if(saved || !failure) goto cleanup;
  g_clear_error(&failure);
  if(!check_secret("test-replacement-password")) goto cleanup;
  puts("PASS: stale drafts are rejected without overwriting newer settings");
  status=0;
cleanup:
  if(remote) {
    GError *cleanup_error=NULL;
    if(!nm_remote_connection_delete(remote,NULL,&cleanup_error)) {
      fprintf(stderr,"Could not remove temporary test profile %s: %s\n",name,cleanup_error->message);
      g_clear_error(&cleanup_error);status=1;
    } else puts("PASS: temporary disconnected test profile removed");
    g_object_unref(remote);
  }
  g_free(name);g_free(uuid);g_object_unref(connection);network_sidebar_actions_unref(actions);g_object_unref(client);
done:
  if(failure) {fprintf(stderr,"Save test failed: %s\n",failure->message);g_error_free(failure);}
  g_main_loop_unref(loop);
  return status;
}
