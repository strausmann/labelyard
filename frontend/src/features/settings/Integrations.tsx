// SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

import { Trans, useLingui } from "@lingui/react/macro";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { Button } from "react-aria-components";
import { PRIMARY_BUTTON } from "../../shell/SignIn";
import { StatusBadge } from "../../shell/StatusBadge";
import { PANEL, SECONDARY_BUTTON } from "../ui";
import { useApiKeys, useSaveSettings, useTestIntegration, type ApiKey, type IntegrationTestResult, type SettingView } from "./api";
import { useInfo } from "../../app/session";
import { sameUrl, useSettingsDraft } from "./draft";
import { LastUsed } from "./LastUsed";
import { SaveFeedback, useErrorText } from "./feedback";
import { GrocyLabelSettings } from "./GrocyLabelSettings";
import { SettingField } from "./SettingField";
import { Section, WithSettings } from "./sections";
import { SPOOLMAN_LABEL_KEYS, SpoolmanLabelFields } from "./SpoolmanLabelFields";
import { webUrlHost, webUrlProblem } from "./webUrl";

type Integration = "snipeit" | "grocy" | "spoolman" | "paperless";
const SECRET_SUFFIX: Record<Integration, string | null> = { snipeit: "api_key", grocy: "api_key", spoolman: null, paperless: "token" };
const NAMES: Record<Integration, string> = { snipeit: "Snipe-IT", grocy: "Grocy", spoolman: "Spoolman", paperless: "Paperless" };
// The order of the approved mockup (M11): the integrations that print items first, Paperless last.
const ORDER: Integration[] = ["snipeit", "grocy", "spoolman", "paperless"];

const isRedirect = (status: number | null | undefined): boolean => status != null && status >= 300 && status < 400;

/** The probe's detail, unless it only repeats the status that is shown next to it ("HTTP 302 HTTP 302"). */
function distinctDetail(result: IntegrationTestResult): string | null {
  const detail = result.detail?.trim();
  if (!detail || (result.status != null && detail === `HTTP ${result.status}`)) return null;
  return detail;
}

function IntegrationCard({ name, views }: { name: Integration; views: SettingView[] }) {
  const { t } = useLingui();
  const errorText = useErrorText();
  const draft = useSettingsDraft(views);
  const save = useSaveSettings();
  const test = useTestIntegration(name);
  const info = useInfo().data?.integrations.find((i) => i.name === name);
  const [saved, setSaved] = useState(false);
  // What the last test ran against: a result only describes the configuration it tested.
  const [testedSignature, setTestedSignature] = useState<string | null>(null);

  const urlKey = `integrations.${name}.url`;
  const secretKey = SECRET_SUFFIX[name] ? `integrations.${name}.${SECRET_SUFFIX[name]}` : null;
  const timeoutKey = `integrations.${name}.timeout`;
  const webUrlKey = `integrations.${name}.web_url`;
  // The connection: what the probe uses. The public web address and the spool label's fields (#346) are saved
  // with the card but are not part of the connection test.
  const connectionKeys = [urlKey, ...(secretKey ? [secretKey] : []), timeoutKey];
  // Paperless prints no items, so its settings carry no web address and the field is not offered.
  const webUrlView = draft.view(webUrlKey);
  const keys = [...connectionKeys, ...(webUrlView ? [webUrlKey] : []), ...(name === "spoolman" ? SPOOLMAN_LABEL_KEYS : [])];
  const urlView = draft.view(urlKey);
  const secretView = secretKey ? draft.view(secretKey) : undefined;
  if (!urlView) return null;

  const urlNow = String(draft.valueOf(urlKey));
  const urlChanged = !urlView.locked && draft.isDirty(urlKey) && urlNow.trim() !== "" && !sameUrl(urlNow, String(urlView.value ?? ""));
  const secretRequired = Boolean(secretView && !secretView.locked && secretView.is_set && urlChanged && draft.valueOf(secretKey!) === "");

  const { values, invalid } = draft.payload(keys);
  const probe = draft.payload(connectionKeys);
  // `changed` says whether the connection differs from what is saved; the web address does not touch a test result.
  const changed = Object.keys(probe.values).length > 0;
  const dirty = Object.keys(values).length > 0;
  const webUrlNow = String(draft.valueOf(webUrlKey) ?? "");
  const webUrlRefusal = webUrlView && !webUrlView.locked ? webUrlProblem(webUrlNow) : null;
  const linksShown = Boolean(webUrlView?.is_set && String(webUrlView.value ?? "") !== "");
  const savedHost = webUrlView ? webUrlHost(String(webUrlView.value ?? "")) : null;
  const savedInSettings = urlView.is_set && (!secretView || secretView.is_set);
  // One source with the sidebar: /api/v1/info says whether the integration is usable (Paperless has no entry there).
  const configured = info ? info.configured : savedInSettings;
  const savedButUnusable = !configured && (urlView.is_set || Boolean(secretView?.is_set));
  // The probe uses URL, secret and timeout. A secret's value is not readable here, only that one is present.
  const signature = JSON.stringify([
    urlNow.trim().replace(/\/+$/, ""),
    String(draft.valueOf(timeoutKey)),
    Boolean(secretView?.is_set) || (secretKey !== null && ![null, ""].includes(draft.valueOf(secretKey) as string | null)),
  ]);
  // A result describes the configuration it tested, not whatever the form shows now (settings may refresh after a save elsewhere).
  const resultCurrent = testedSignature === signature;
  // A test still running for an older configuration neither blocks the button nor shows a result.
  const testing = test.isPending && resultCurrent;
  // The header says what is known. "Connected" and "Connection failed" come only from a test of the values
  // that are saved; a result for unsaved edits says nothing about the saved integration.
  const tested = test.isSuccess && !changed && resultCurrent ? test.data : null;
  const header: { state: "ok" | "bad" | "idle"; label: string } = tested
    ? tested.ok
      ? { state: "ok", label: t`Connected` }
      : { state: "bad", label: t`Connection failed` }
    : configured
      ? { state: "ok", label: t`Configured` }
      : { state: "idle", label: t`Not set up` };
  const refusal = (problem: NonNullable<ReturnType<typeof webUrlProblem>>) =>
    ({
      scheme: t`Use an http or https address without a user name or password.`,
      credentials: t`Use an http or https address without a user name or password.`,
      host: t`Add the host name after https://.`,
      query: t`Leave out the part after “?” or “#”; the hub adds the item's path itself.`,
      length: t`Use at most 2048 characters.`,
      invalid: t`This is not a valid web address.`,
    })[problem];
  const label = (suffix: string) => ({ url: t`Address the hub uses`, secret: SECRET_SUFFIX[name] === "token" ? t`Token` : t`API key`, timeout: t`Timeout (seconds)` })[suffix]!;

  // The variable that sets a value of this integration, as the operator writes it (LABELYARD_SNIPEIT_URL).
  const envName = (suffix: string) => `LABELYARD_${name.toUpperCase()}_${suffix}`;

  const change = (key: string) => (value: string | boolean | null) => {
    setSaved(false);
    // The web address is not part of the connection: editing it leaves the test result as it is.
    if (key !== webUrlKey) test.reset();
    draft.set(key, value);
  };

  const heading = `${name}-heading`;
  return (
    <div role="group" aria-labelledby={heading} className={`${PANEL} flex flex-col gap-5`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id={heading} className="text-section font-bold">
          {NAMES[name]}
        </h3>
        {webUrlView ? <StatusBadge state={webUrlRefusal ? "bad" : linksShown ? "ok" : "idle"} label={webUrlRefusal ? t`Not saved` : linksShown ? t`Links shown` : t`No links shown`} /> : null}
      </div>
      <p className="-mt-3 text-small text-ink-2">{header.label}</p>
      {savedButUnusable ? (
        <p className="max-w-xl text-small text-ink-2">
          {t`Saved values exist, but this integration cannot be used yet. A secret may belong to another URL or may not be readable with the current secret key, or the URL is missing. Enter the secret again.`}
        </p>
      ) : null}
      <div className="grid max-w-xl gap-5">
        <SettingField
          view={urlView}
          label={label("url")}
          value={draft.valueOf(urlKey)}
          onChange={change(urlKey)}
          description={t`Internal. Only admins see this address.`}
          envName={envName("URL")}
        />
        {webUrlView ? (
          <div className="flex flex-col gap-2">
            <SettingField
              view={webUrlView}
              label={t`Public web address`}
              value={draft.valueOf(webUrlKey)}
              onChange={change(webUrlKey)}
              description={t`The address people reach ${NAMES[name]} on. The hub builds the links in the app from it, and — once it is set — the QR codes on printed labels too. Empty: no link is shown, and printed codes keep working as they do now.`}
              error={webUrlRefusal ? refusal(webUrlRefusal) : null}
              envName={envName("WEB_URL")}
            />
            {savedHost && !draft.isDirty(webUrlKey) ? (
              <p className="text-small text-ink-2">
                <Trans>
                  Links open on <span className="font-mono text-ink">{savedHost}</span>
                </Trans>
              </p>
            ) : null}
            {!webUrlView.locked && (webUrlNow !== "" || linksShown) ? (
              <Button onPress={() => change(webUrlKey)("")} className={`${SECONDARY_BUTTON} self-start`}>
                {t({ message: "Clear", context: "public web address" })}
              </Button>
            ) : null}
          </div>
        ) : null}
        {secretView && secretKey ? (
          <SettingField view={secretView} label={label("secret")} value={draft.valueOf(secretKey)} onChange={change(secretKey)} required={secretRequired} />
        ) : null}
        {draft.view(timeoutKey) ? (
          <SettingField view={draft.view(timeoutKey)!} label={label("timeout")} value={draft.valueOf(timeoutKey)} onChange={change(timeoutKey)} />
        ) : null}
        {name === "spoolman" ? <SpoolmanLabelFields draft={draft} /> : null}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button
          onPress={() => {
            setTestedSignature(signature);
            test.mutate(probe.values);
          }}
          isDisabled={testing || probe.invalid.length > 0}
          className={SECONDARY_BUTTON}
        >
          {testing ? t`Testing…` : t`Test connection`}
        </Button>
        <Button
          isDisabled={!dirty || invalid.length > 0 || webUrlRefusal !== null || secretRequired || save.isPending}
          onPress={() => {
            setSaved(false);
            save.mutate(values, {
              onSuccess: () => {
                draft.clear(keys);
                setSaved(true);
              },
            });
          }}
          className={PRIMARY_BUTTON}
        >
          {t`Save`}
        </Button>
      </div>
      {test.isPending || !resultCurrent ? null : test.isSuccess ? (
        <div role="status" className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className={`text-small font-bold ${test.data.ok ? "text-ok" : "text-bad"}`}>{test.data.ok ? t`Works` : t`Failed`}</span>
          {test.data.ok ? null : (
            <>
              {test.data.status ? <span className="font-mono text-small text-ink-2">{t`HTTP ${test.data.status}`}</span> : null}
              {isRedirect(test.data.status) ? (
                <span className="text-ink">{t`The address redirects, often to a login page in front of the app. Use an address that reaches the app's API directly.`}</span>
              ) : distinctDetail(test.data) ? (
                <span className="text-ink">{distinctDetail(test.data)}</span>
              ) : null}
            </>
          )}
        </div>
      ) : test.isError ? (
        <p role="alert">{errorText(test.error, t`The test could not be run.`)}</p>
      ) : null}
      <SaveFeedback error={save.isError ? errorText(save.error) : null} saved={saved} />
    </div>
  );
}

/** Keys whose names identify a client of this hub (Kept, Hangar …); admin keys are not clients. */
const CLIENT_PATTERN = /kept|hangar|grocy|spoolman|paperless|snipe/i;

export const isClientKey = (key: ApiKey): boolean => key.enabled && CLIENT_PATTERN.test(key.name);

function ClientList() {
  const { t } = useLingui();
  const keys = useApiKeys();
  const clients = (keys.data ?? []).filter(isClientKey);
  return (
    <Section
      title={t`Clients`}
      label="Clients"
      intro={t`Other apps that print through this hub. They are recognised by the name of their API key.`}
    >
      {keys.isPending ? (
        <p role="status" className="text-ink-2">{t`Loading…`}</p>
      ) : clients.length === 0 ? (
        <p className="text-ink-2">
          <Trans>
            No client keys yet. Create one under{" "}
            <Link to="/settings/access" className="text-link underline underline-offset-2">
              Access and keys
            </Link>
            , named after the client (for example “Kept”).
          </Trans>
        </p>
      ) : (
        <ul className="grid gap-2">
          {clients.map((key) => (
            <li key={key.id} className={`${PANEL} flex flex-wrap items-center gap-x-6 gap-y-1`}>
              <span className="min-w-32 font-bold">{key.name}</span>
              <span className="flex gap-2">
                {key.scopes.map((scope) => (
                  <span key={scope} className="rounded-r1 bg-sunken px-2 py-0.5 text-small">
                    {scope}
                  </span>
                ))}
              </span>
              <span className="text-small text-ink-2">
                <LastUsed at={key.last_used_at} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

/** What the public web address is for (M11): shown once above the cards, not repeated in each. */
function WebAddressNote() {
  const { t } = useLingui();
  return (
    <div role="note" className="flex gap-3 rounded-r2 bg-info-soft p-3 text-small text-ink">
      <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true" focusable="false" className="mt-0.5 shrink-0 fill-none stroke-link" strokeWidth="1.6">
        <circle cx="10" cy="10" r="8" />
        <path d="M10 9v5M10 6.2v.1" />
      </svg>
      <div className="flex min-w-0 flex-col gap-0.5">
        <strong>{t`Public web address`}</strong>
        <span>{t`Where an item's page lives for people, for example on your own domain. Without it the hub shows no "Open in" link. It never changes the address the hub itself uses to reach the integration.`}</span>
      </div>
    </div>
  );
}

export function Integrations() {
  const { t } = useLingui();
  return (
    <div className="flex flex-col gap-10">
      <Section
        title={t`Integrations`}
        intro={t`Connect the apps that labels are made from. A secret belongs to the URL it was saved with: change the URL and you enter the secret again.`}
      >
        <WithSettings>
          {(views) => (
            <div className="flex flex-col gap-5">
              <WebAddressNote />
              <div className="grid gap-5 xl:grid-cols-2">
                {ORDER.map((name) => (
                  <IntegrationCard key={name} name={name} views={views} />
                ))}
              </div>
              <GrocyLabelSettings views={views} />
            </div>
          )}
        </WithSettings>
      </Section>
      <ClientList />
    </div>
  );
}
