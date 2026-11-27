import { useMemo, useRef, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  Bot,
  Check,
  Globe,
  KeyRound,
  LogIn,
  RefreshCw,
  Search,
  ShieldCheck,
  Wrench,
  X,
} from 'lucide-react';
import { api } from '../api';
import type {
  Agent,
  AvailableModel,
  Capability,
  ProviderLoginResponse,
  ProviderStatus,
  RolodexEntry,
  SkillSummary,
  UsageSummary,
} from '../types';

interface ContainerRegistryProps {
  agents: Agent[];
  capabilities: Capability[];
  providers: ProviderStatus[];
  usage: UsageSummary | null;
  rolodex: RolodexEntry[];
  skills: SkillSummary[];
  skillSources?: Record<string, number>;
  onReload: () => Promise<void>;
  onRefreshRegistry: () => Promise<void>;
}

interface ConfigModalState {
  provider: string;
  displayName: string;
  supportsApiKey: boolean;
  supportsBaseUrl: boolean;
  requiresApiKey: boolean;
  existingBaseUrl: string;
  isTool: boolean;
}

const STATUS_DOT: Record<string, string> = {
  authenticated: 'green',
  configured: 'green',
  api_key_configured: 'green',
  expired: 'yellow',
  unauthenticated: 'muted',
  unconfigured: 'muted',
  missing_binary: 'red',
  unreachable: 'red',
  error: 'red',
};

const STATUS_LABEL: Record<string, string> = {
  authenticated: 'Authenticated',
  configured: 'Configured',
  api_key_configured: 'Configured',
  expired: 'Expired',
  unauthenticated: 'Unauthenticated',
  unconfigured: 'Unconfigured',
  missing_binary: 'Missing Binary',
  unreachable: 'Unreachable',
  error: 'Error',
};

const MAX_VISIBLE_PROVIDER_MODELS = 4;

function stripAnsi(text: string): string {
  // Strip ANSI escape sequences (CSI and OSC) emitted by provider CLIs.
  // eslint-disable-next-line no-control-regex
  return text.replace(/\u001b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\u0007]*(?:\u0007|\u001b\\))/g, '');
}

function isConfiguredStatus(status: string): boolean {
  return status === 'authenticated' || status === 'configured' || status === 'api_key_configured';
}

function formatTimestamp(ts?: string | null): string {
  if (!ts) return '';
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function isToolProvider(p: ProviderStatus): boolean {
  return p.category === 'tool' || p.provider_id.toLowerCase() === 'tavily';
}

function countAvailableLlmModels(
  models?: AvailableModel[] | null,
  fallbackCount?: number,
): number | undefined {
  if (Array.isArray(models)) {
    return models.filter(m => m.available && m.is_llm !== false).length;
  }
  return fallbackCount;
}

function resolveServiceProvider(item: {
  provider: string;
  service_provider?: string | null;
  container_id?: string;
  base_url?: string | null;
  model?: string | null;
  cost_tier?: string | null;
}): string {
  if (item.service_provider && item.service_provider.trim()) {
    return item.service_provider.trim().toLowerCase();
  }
  const provider = (item.provider || '').trim().toLowerCase();
  if (provider !== 'api') return provider;
  const cid = (item.container_id || '').trim().toLowerCase();
  const burl = (item.base_url || '').trim().toLowerCase();
  const model = (item.model || '').trim().toLowerCase();
  if (cid.includes('groq') || burl.includes('groq.com')) return 'groq';
  if (cid.includes('tavily') || model === 'tavily-search') return 'tavily';
  if (item.cost_tier === 'local_free' || cid === 'local_triage') return 'local';
  if (burl) {
    try {
      const host = new URL(burl).hostname.toLowerCase();
      if (
        (host === '127.0.0.1' || host === 'localhost' || host === '::1' || host === '[::1]') &&
        !cid.includes('custom')
      ) {
        return 'local';
      }
    } catch {
      // Ignore malformed URLs during display inference.
    }
  }
  return 'api';
}

export default function ContainerRegistry({
  agents,
  capabilities,
  providers,
  usage,
  rolodex,
  skills,
  skillSources = {},
  onReload,
  onRefreshRegistry,
}: ContainerRegistryProps) {
  const [refreshBusy, setRefreshBusy] = useState(false);
  const [providerRefreshBusy, setProviderRefreshBusy] = useState<string | null>(null);
  const [loginBusy, setLoginBusy] = useState<string | null>(null);
  const [loginResult, setLoginResult] = useState<ProviderLoginResponse | null>(null);
  const [callbackInput, setCallbackInput] = useState('');
  const [completeBusy, setCompleteBusy] = useState(false);
  const [completeError, setCompleteError] = useState('');
  const [completeSuccess, setCompleteSuccess] = useState('');
  const [configModal, setConfigModal] = useState<ConfigModalState | null>(null);
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [baseUrlInput, setBaseUrlInput] = useState('');
  const [modalError, setModalError] = useState('');
  const [keyBusy, setKeyBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [downshiftBusy, setDownshiftBusy] = useState<string | null>(null);
  const [toggleBusy, setToggleBusy] = useState<string | null>(null);
  const [expandedProviders, setExpandedProviders] = useState<Record<string, boolean>>({});

  // Rolodex filters
  const [search, setSearch] = useState('');
  const [providerFilter, setProviderFilter] = useState('all');
  const [tierFilter, setTierFilter] = useState('all');

  // Capabilities filters
  const [capProviderFilter, setCapProviderFilter] = useState('all');
  const [capAvailFilter, setCapAvailFilter] = useState<'all' | 'available' | 'unavailable'>('all');
  const capabilitiesRef = useRef<HTMLDivElement | null>(null);

  // Skills filters
  const [skillSearch, setSkillSearch] = useState('');
  const [skillSourceFilter, setSkillSourceFilter] = useState<'all' | 'skillforge' | 'dagger_local' | 'workspace'>('all');

  const openConfigModal = (next: ConfigModalState) => {
    setApiKeyInput('');
    setBaseUrlInput(next.existingBaseUrl);
    setModalError('');
    setActionError('');
    setConfigModal(next);
  };

  const closeConfigModal = () => {
    setConfigModal(null);
    setApiKeyInput('');
    setBaseUrlInput('');
    setModalError('');
  };

  const openProviderConfigModal = (p: ProviderStatus) => {
    const pid = p.provider_id.toLowerCase();
    const supportsBaseUrl = Boolean(p.supports_base_url || pid === 'api' || pid === 'local');
    const requiresApiKey =
      p.requires_api_key !== undefined
        ? p.requires_api_key
        : Boolean(p.supports_api_key && pid !== 'local');
    openConfigModal({
      provider: p.provider_id,
      displayName: p.display_name,
      supportsApiKey: p.supports_api_key,
      supportsBaseUrl,
      requiresApiKey,
      existingBaseUrl: p.base_url ?? '',
      isTool: isToolProvider(p),
    });
  };

  const openCustomApiModal = () => {
    const existing = providers.find(p => p.provider_id.toLowerCase() === 'api');
    if (existing) {
      openProviderConfigModal(existing);
      return;
    }
    openConfigModal({
      provider: 'api',
      displayName: 'Custom OpenAI-Compatible API',
      supportsApiKey: true,
      supportsBaseUrl: true,
      requiresApiKey: true,
      existingBaseUrl: '',
      isTool: false,
    });
  };

  const getProviderModels = (p: ProviderStatus): AvailableModel[] => {
    if (Array.isArray(p.available_models) && (p.available_models.length > 0 || p.discovery_status !== undefined)) {
      return p.available_models;
    }
    const pid = p.provider_id.toLowerCase();
    return capabilities
      .filter(cap => {
        const capProvider = (cap.service_provider || cap.provider).toLowerCase();
        return capProvider === pid;
      })
      .map(cap => ({
        provider_id: p.provider_id,
        model_id: cap.model_name,
        display_name: cap.display_name ?? null,
        available: cap.available ?? cap.healthy,
        is_llm: cap.is_llm ?? true,
        context_window: cap.max_context_tokens,
        discovery_source: cap.discovery_source,
        last_checked: cap.last_checked ?? null,
      }));
  };

  const modelAvailabilityIndex = useMemo(() => {
    const availableKeys = new Set<string>();
    const toolReadyKeys = new Set<string>();
    const unavailableKeys = new Set<string>();
    const verifiedProviders = new Set<string>();
    const providerHealthById = new Map<string, string>();
    const toolProviderIds = new Set<string>();

    const registerKey = (target: Set<string>, providerKey: string, modelValue?: string | null) => {
      if (!providerKey || !modelValue) return;
      const pKey = providerKey.trim().toLowerCase();
      const mKey = modelValue.trim().toLowerCase();
      if (!pKey || !mKey) return;
      target.add(`${pKey}::${mKey}`);
    };

    for (const p of providers) {
      const pid = p.provider_id.toLowerCase();
      providerHealthById.set(pid, p.status);
      if (isToolProvider(p)) {
        toolProviderIds.add(pid);
      }
      if (
        p.discovery_status === 'ok' ||
        p.discovery_status === 'fallback' ||
        (Array.isArray(p.available_models) && p.available_models.length > 0)
      ) {
        verifiedProviders.add(pid);
      }
      for (const m of p.available_models ?? []) {
        const provId = (m.provider_id || pid).toLowerCase();
        const bucket = !m.available
          ? unavailableKeys
          : m.is_llm === false
            ? toolReadyKeys
            : availableKeys;
        registerKey(bucket, provId, m.model_id);
        if (m.display_name) registerKey(bucket, provId, m.display_name);
      }
    }

    for (const cap of capabilities) {
      const isAvail = cap.available ?? cap.healthy;
      const pPrimary = (cap.service_provider || cap.provider).toLowerCase();
      const bucket = !isAvail
        ? unavailableKeys
        : cap.is_llm === false
          ? toolReadyKeys
          : availableKeys;
      registerKey(bucket, pPrimary, cap.model_name);
      if (cap.display_name) {
        registerKey(bucket, pPrimary, cap.display_name);
      }
      verifiedProviders.add(pPrimary);
    }

    return {
      availableKeys,
      toolReadyKeys,
      unavailableKeys,
      verifiedProviders,
      providerHealthById,
      toolProviderIds,
    };
  }, [providers, capabilities]);

  const getModelAvailability = (
    serviceProvider: string,
    model?: string | null,
  ): 'ready' | 'tool_ready' | 'unavailable' | 'unverified' => {
    const pKey = (serviceProvider || '').trim().toLowerCase();
    const mKey = (model || '').trim().toLowerCase();
    if (!pKey || !mKey) return 'unverified';
    const pair = `${pKey}::${mKey}`;
    if (modelAvailabilityIndex.toolReadyKeys.has(pair)) return 'tool_ready';
    if (modelAvailabilityIndex.availableKeys.has(pair)) return 'ready';
    if (modelAvailabilityIndex.unavailableKeys.has(pair)) return 'unavailable';
    const provStatus = modelAvailabilityIndex.providerHealthById.get(pKey);
    if (modelAvailabilityIndex.toolProviderIds.has(pKey)) {
      if (!provStatus) return 'unverified';
      return isConfiguredStatus(provStatus) ? 'tool_ready' : 'unavailable';
    }
    if (provStatus && !isConfiguredStatus(provStatus)) return 'unavailable';
    if (modelAvailabilityIndex.verifiedProviders.has(pKey)) return 'unavailable';
    return 'unverified';
  };

  const skillCounts = useMemo(() => {
    return {
      total: skills.length,
      skillforge: skillSources.skillforge ?? skills.filter(s => s.source === 'skillforge').length,
      dagger_local: skillSources.dagger_local ?? skills.filter(s => s.source === 'dagger_local').length,
      workspace: skillSources.workspace ?? skills.filter(s => s.source === 'workspace').length,
    };
  }, [skills, skillSources]);

  const filteredSkills = useMemo(() => {
    const q = skillSearch.trim().toLowerCase();
    return skills.filter(skill => {
      if (skillSourceFilter !== 'all' && skill.source !== skillSourceFilter) return false;
      if (!q) return true;
      return (
        skill.name.toLowerCase().includes(q) ||
        skill.description.toLowerCase().includes(q) ||
        skill.tags.some(t => t.toLowerCase().includes(q))
      );
    });
  }, [skills, skillSearch, skillSourceFilter]);

  const capabilityStats = useMemo(() => {
    let available = 0;
    let unavailable = 0;
    const providerIds = new Set<string>();
    for (const cap of capabilities) {
      providerIds.add(cap.service_provider || cap.provider);
      if (cap.available ?? cap.healthy) available += 1;
      else unavailable += 1;
    }
    return {
      available,
      unavailable,
      providers: [...providerIds].sort(),
    };
  }, [capabilities]);

  const filteredCapabilities = useMemo(() => {
    return capabilities.filter(cap => {
      const capProv = cap.service_provider || cap.provider;
      if (capProviderFilter !== 'all' && capProv.toLowerCase() !== capProviderFilter.toLowerCase()) {
        return false;
      }
      const isAvail = cap.available ?? cap.healthy;
      if (capAvailFilter === 'available' && !isAvail) return false;
      if (capAvailFilter === 'unavailable' && isAvail) return false;
      return true;
    });
  }, [capabilities, capProviderFilter, capAvailFilter]);

  const jumpToCapabilities = (providerId: string) => {
    setCapProviderFilter(providerId);
    setCapAvailFilter('available');
    capabilitiesRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  const formatDiscoveryBanner = (displayName: string, count?: number, isTool?: boolean, verb = 'Configured') => {
    if (isTool) {
      return `${verb} ${displayName} · Search tool ready for agents.`;
    }
    if (typeof count === 'number' && count > 0) {
      return `${verb} ${displayName} · ${count} ${count === 1 ? 'model' : 'models'} available for Agent Builder & Orchestrators.`;
    }
    return `${verb} ${displayName}.`;
  };

  const handleRefreshRegistry = async () => {
    setRefreshBusy(true);
    setActionError('');
    try {
      await onRefreshRegistry();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Could not refresh registry.');
    } finally {
      setRefreshBusy(false);
    }
  };

  const handleRefreshProvider = async (p: ProviderStatus) => {
    setProviderRefreshBusy(p.provider_id);
    setActionError('');
    try {
      const updated = await api.refreshProvider(p.provider_id);
      await onReload();
      if (updated.discovery_error) {
        setActionError(`${updated.display_name || p.display_name}: ${updated.discovery_error}`);
      } else {
        const count = countAvailableLlmModels(updated.available_models, updated.available_model_count);
        setCompleteSuccess(
          formatDiscoveryBanner(
            updated.display_name || p.display_name,
            count,
            isToolProvider(updated),
            'Discovered',
          ),
        );
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : `Could not discover models for ${p.display_name}.`);
    } finally {
      setProviderRefreshBusy(null);
    }
  };

  const handleLogin = async (providerId: string) => {
    setLoginBusy(providerId);
    setLoginResult(null);
    setActionError('');
    setCallbackInput('');
    setCompleteError('');
    setCompleteSuccess('');
    try {
      const res = await api.providerLogin(providerId);
      if (res.status === 'authenticated' || res.status === 'already_authenticated') {
        const count = countAvailableLlmModels(
          res.provider_status?.available_models ?? res.available_models,
          res.provider_status?.available_model_count,
        );
        const displayName =
          res.provider_status?.display_name ??
          providers.find(p => p.provider_id === providerId)?.display_name ??
          providerId;
        setCompleteSuccess(formatDiscoveryBanner(displayName, count, false, 'Signed in to'));
        await onReload();
      } else {
        setLoginResult(res);
        // Do NOT run refreshRegistry while waiting for user OAuth callback.
        if (res.status !== 'awaiting_callback' && res.status !== 'manual_action_required') {
          await onReload();
        }
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Login request failed.');
    } finally {
      setLoginBusy(null);
    }
  };

  const submitLoginCompletion = async (input: string) => {
    if (!loginResult) return;
    setCompleteBusy(true);
    setCompleteError('');
    setCompleteSuccess('');
    try {
      const updated =
        loginResult.status === 'manual_action_required' && !input
          ? await api.refreshProvider(loginResult.provider_id)
          : await api.completeProviderLogin(loginResult.provider_id, input);
      if (isConfiguredStatus(updated.status)) {
        const count = countAvailableLlmModels(updated.available_models, updated.available_model_count);
        setLoginResult(null);
        setCallbackInput('');
        setCompleteSuccess(
          formatDiscoveryBanner(
            updated.display_name || updated.provider_id,
            count,
            isToolProvider(updated),
            'Signed in to',
          ),
        );
      } else {
        setCompleteError(updated.detail || `Sign-in did not complete (status: ${updated.status}).`);
      }
      await onReload();
    } catch (err) {
      setCompleteError(err instanceof Error ? err.message : 'Failed to complete sign-in.');
    } finally {
      setCompleteBusy(false);
    }
  };

  const handleCompleteLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!loginResult || !callbackInput.trim()) return;
    await submitLoginCompletion(callbackInput.trim());
  };

  const handleSaveConfig = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!configModal) return;
    const trimmedKey = apiKeyInput.trim();
    const trimmedUrl = baseUrlInput.trim();
    if (configModal.requiresApiKey && !trimmedKey) {
      setModalError('API key is required for this provider.');
      return;
    }
    if (!configModal.requiresApiKey && configModal.supportsBaseUrl && !trimmedUrl && !configModal.existingBaseUrl) {
      setModalError('Please enter a Base URL for this endpoint.');
      return;
    }

    setKeyBusy(true);
    setModalError('');
    setActionError('');
    setCompleteSuccess('');
    try {
      const res = await api.saveProviderCredentials(
        configModal.provider,
        configModal.supportsApiKey || configModal.requiresApiKey ? trimmedKey : '',
        configModal.supportsBaseUrl ? trimmedUrl : undefined,
      );
      const count = countAvailableLlmModels(
        res.provider_status?.available_models ?? res.available_models,
        res.provider_status?.available_model_count ?? res.available_model_count,
      );
      const displayName = configModal.displayName;
      const isTool = configModal.isTool;
      closeConfigModal();
      setCompleteSuccess(formatDiscoveryBanner(displayName, count, isTool, 'Configured'));
      await onReload();
    } catch (err) {
      setModalError(err instanceof Error ? err.message : 'Could not save credentials.');
    } finally {
      setKeyBusy(false);
    }
  };

  const handleDownshift = async (
    containerId: string,
    targetModel?: string,
    targetContainerId?: string,
  ) => {
    setDownshiftBusy(containerId);
    setActionError('');
    try {
      await api.downshiftContainer(containerId, {
        target_model: targetModel,
        target_container_id: targetContainerId,
      });
      await onReload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Downshift failed.');
    } finally {
      setDownshiftBusy(null);
    }
  };

  const handleToggleActive = async (containerId: string) => {
    const agent = agents.find(a => a.container_id === containerId);
    if (!agent) return;
    setToggleBusy(containerId);
    setActionError('');
    try {
      await api.toggleAgentActive(agent);
      await onReload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Could not update container status.');
    } finally {
      setToggleBusy(null);
    }
  };

  const rolodexProviders = useMemo(
    () => [...new Set(rolodex.map(r => resolveServiceProvider(r)))].sort(),
    [rolodex],
  );
  const rolodexTiers = useMemo(
    () => [...new Set(rolodex.map(r => r.cost_tier))].sort(),
    [rolodex],
  );

  const filteredRolodex = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rolodex.filter(entry => {
      const sp = resolveServiceProvider(entry);
      if (providerFilter !== 'all' && sp !== providerFilter) return false;
      if (tierFilter !== 'all' && entry.cost_tier !== tierFilter) return false;
      if (!q) return true;
      return (
        entry.container_id.toLowerCase().includes(q) ||
        sp.includes(q) ||
        (entry.model ?? '').toLowerCase().includes(q) ||
        entry.role.toLowerCase().includes(q) ||
        entry.skills.some(s => s.toLowerCase().includes(q))
      );
    });
  }, [rolodex, search, providerFilter, tierFilter]);

  const maxTrendCost = useMemo(() => {
    if (!usage?.metered.daily_trend.length) return 1;
    return Math.max(...usage.metered.daily_trend.map(d => d.cost_usd), 0.0001);
  }, [usage]);

  const saveDisabled =
    keyBusy ||
    !configModal ||
    (configModal.requiresApiKey && !apiKeyInput.trim()) ||
    (!configModal.requiresApiKey && configModal.supportsBaseUrl && !baseUrlInput.trim() && !configModal.existingBaseUrl);

  return (
    <>
      {actionError && (
        <div className="error-banner" role="alert">
          {actionError}
          <button aria-label="Dismiss error" onClick={() => setActionError('')}>
            <X size={15} />
          </button>
        </div>
      )}

      {/* 1. Providers & Subscriptions */}
      <div className="registry-heading">
        <div>
          <h2>Providers &amp; subscriptions</h2>
          <p>Register providers, discover available models, and verify readiness for Agent Builder &amp; Orchestrators</p>
        </div>
        <div className="registry-actions-row">
          <button className="secondary-button" onClick={openCustomApiModal}>
            <KeyRound size={14} /> Custom API Key
          </button>
          <button
            className="secondary-button"
            disabled={refreshBusy}
            onClick={() => void handleRefreshRegistry()}
          >
            <RefreshCw size={14} className={refreshBusy ? 'spin' : ''} />
            {refreshBusy ? 'Refreshing…' : 'Refresh registry'}
          </button>
        </div>
      </div>

      {completeSuccess && (
        <div className="panel login-banner success-banner">
          <div className="login-banner-body">
            <strong>Provider ready</strong>
            <span>{completeSuccess}</span>
          </div>
          <button className="icon-button" aria-label="Dismiss provider notice" onClick={() => setCompleteSuccess('')}>
            <X size={15} />
          </button>
        </div>
      )}

      {loginResult && (() => {
        const authUrl = loginResult.auth_url || loginResult.device_url;
        const awaitingCallback =
          loginResult.status === 'awaiting_callback' || Boolean(loginResult.requires_callback);
        const manualAction = loginResult.status === 'manual_action_required';
        const cleanedMessage = stripAnsi(loginResult.message || '').trim();
        return (
          <div className="panel login-banner">
            <div className="login-banner-body">
              <strong>
                {loginResult.provider_id} · {loginResult.status.replace(/_/g, ' ')}
              </strong>
              {cleanedMessage && <pre className="login-message">{cleanedMessage}</pre>}
              {(authUrl || loginResult.device_code) && (
                <div className="device-auth-row">
                  {authUrl && (
                    <a
                      href={authUrl}
                      target="_blank"
                      rel="noreferrer"
                      className="device-link"
                    >
                      {authUrl}
                    </a>
                  )}
                  {loginResult.device_code && (
                    <code className="device-code">{loginResult.device_code}</code>
                  )}
                </div>
              )}
              {awaitingCallback && (
                <form className="callback-handoff-form" onSubmit={e => void handleCompleteLogin(e)}>
                  <label htmlFor="provider-callback-input">
                    Paste authorization code or localhost callback URL (or check status after browser/device sign-in)
                  </label>
                  <div className="callback-handoff-row">
                    <input
                      id="provider-callback-input"
                      type="text"
                      value={callbackInput}
                      onChange={e => setCallbackInput(e.target.value)}
                      placeholder="http://localhost:... or authorization code"
                      disabled={completeBusy}
                    />
                    <button
                      type="submit"
                      className="primary-button compact-btn"
                      disabled={!callbackInput.trim() || completeBusy}
                    >
                      {completeBusy ? 'Completing…' : 'Complete Sign-In'}
                    </button>
                    <button
                      type="button"
                      className="secondary-button compact-btn"
                      disabled={completeBusy}
                      onClick={() => void submitLoginCompletion('')}
                    >
                      <Check size={13} />
                      {completeBusy ? 'Checking…' : 'Check Login Status'}
                    </button>
                  </div>
                  {completeError && <div className="error-text">{completeError}</div>}
                </form>
              )}
              {manualAction && !awaitingCallback && (
                <div className="callback-handoff-form">
                  <div className="callback-handoff-row">
                    <button
                      type="button"
                      className="primary-button compact-btn"
                      disabled={completeBusy}
                      onClick={() => void submitLoginCompletion('')}
                    >
                      <RefreshCw size={13} className={completeBusy ? 'spin' : ''} />
                      {completeBusy ? 'Checking…' : 'Check Login Status'}
                    </button>
                  </div>
                  {completeError && <div className="error-text">{completeError}</div>}
                </div>
              )}
            </div>
            <button
              className="icon-button"
              aria-label="Dismiss login message"
              onClick={() => {
                setLoginResult(null);
                setCallbackInput('');
                setCompleteError('');
              }}
            >
              <X size={15} />
            </button>
          </div>
        );
      })()}

      <div className="provider-grid">
        {providers.map(p => {
          const toolOnly = isToolProvider(p);
          const models = getProviderModels(p);
          const availableModels = models.filter(m => m.available && m.is_llm !== false);
          const availableCount = Array.isArray(p.available_models)
            ? availableModels.length
            : (p.available_model_count ?? availableModels.length);
          const expanded = Boolean(expandedProviders[p.provider_id]);
          const visibleModels = expanded
            ? availableModels
            : availableModels.slice(0, MAX_VISIBLE_PROVIDER_MODELS);
          const hiddenCount = availableModels.length - visibleModels.length;
          const canConfigure = Boolean(
            p.supports_api_key || p.supports_base_url || p.provider_id === 'local' || p.provider_id === 'api',
          );
          const isEndpointOnly = Boolean(!p.supports_api_key && (p.supports_base_url || p.provider_id === 'local'));
          const configured = isConfiguredStatus(p.status);
          const missingBinary = p.status === 'missing_binary';

          return (
            <article className="panel provider-card" key={p.provider_id}>
              <div className="agent-card-top">
                <div className="provider-title-group">
                  {toolOnly ? (
                    <Wrench size={16} className="provider-shield" />
                  ) : p.kind === 'local' ? (
                    <Globe size={16} className="provider-shield" />
                  ) : (
                    <ShieldCheck size={16} className="provider-shield" />
                  )}
                  <strong>{p.display_name}</strong>
                </div>
                <span className="pill">
                  <span className={`status-dot ${STATUS_DOT[p.status] || 'muted'}`} />
                  {STATUS_LABEL[p.status] || p.status}
                </span>
              </div>

              <div className="provider-detail">{p.detail || p.kind}</div>
              {p.base_url && (
                <div className="provider-base-url" title={p.base_url}>
                  <code>{p.base_url}</code>
                </div>
              )}

              <div className="provider-models-block">
                {toolOnly ? (
                  <div className="provider-tool-badge">
                    <Wrench size={11} />
                    <span>Tool / Search provider</span>
                  </div>
                ) : (
                  <>
                    <div className="provider-models-header">
                      {availableCount > 0 ? (
                        <button
                          type="button"
                          className="provider-ready-badge"
                          onClick={() => jumpToCapabilities(p.provider_id)}
                          title="Filter Endpoint Capabilities to this provider's available models"
                        >
                          <span className="status-dot green" />
                          {availableCount} available · Builder Ready
                        </button>
                      ) : (
                        <span className="provider-models-empty">
                          {configured
                            ? 'No models discovered yet'
                            : missingBinary
                              ? 'Install CLI binary on PATH to enable'
                              : 'Register to discover available models'}
                        </span>
                      )}
                      {p.discovery_status && p.discovery_status !== 'unverified' && (
                        <span className={`discovery-badge ${p.discovery_status}`}>
                          {p.discovery_status}
                        </span>
                      )}
                    </div>

                    {p.discovery_error && (
                      <div className="provider-discovery-warn" role="note">
                        <AlertTriangle size={12} />
                        <span>{p.discovery_error}</span>
                      </div>
                    )}

                    {availableModels.length > 0 && (
                      <div className="provider-model-chips">
                        {visibleModels.map(model => (
                          <span
                            key={`${model.provider_id}:${model.model_id}`}
                            className="provider-model-chip"
                            title={`${model.model_id}${model.discovery_source ? ` (${model.discovery_source})` : ''}`}
                          >
                            {model.display_name || model.model_id}
                          </span>
                        ))}
                        {hiddenCount > 0 && (
                          <button
                            type="button"
                            className="provider-model-more"
                            onClick={() =>
                              setExpandedProviders(prev => ({ ...prev, [p.provider_id]: true }))
                            }
                          >
                            +{hiddenCount} more
                          </button>
                        )}
                        {expanded && availableModels.length > MAX_VISIBLE_PROVIDER_MODELS && (
                          <button
                            type="button"
                            className="provider-model-more"
                            onClick={() =>
                              setExpandedProviders(prev => ({ ...prev, [p.provider_id]: false }))
                            }
                          >
                            Show less
                          </button>
                        )}
                      </div>
                    )}
                  </>
                )}
              </div>

              <div className="provider-card-actions">
                {p.supports_login && (
                  <button
                    className="secondary-button compact-btn"
                    disabled={loginBusy === p.provider_id || missingBinary}
                    title={
                      missingBinary
                        ? `Install the ${p.provider_id} CLI binary on PATH before signing in`
                        : 'Start interactive CLI sign-in'
                    }
                    onClick={() => void handleLogin(p.provider_id)}
                  >
                    <LogIn size={13} />
                    {loginBusy === p.provider_id ? 'Starting…' : 'Sign In / Re-Auth'}
                  </button>
                )}
                {canConfigure && (
                  <button
                    className="secondary-button compact-btn"
                    onClick={() => openProviderConfigModal(p)}
                  >
                    {isEndpointOnly ? <Globe size={13} /> : <KeyRound size={13} />}
                    {isEndpointOnly
                      ? p.base_url || configured
                        ? 'Configure Endpoint'
                        : 'Register Endpoint'
                      : configured
                        ? 'Update Key'
                        : 'Add API Key'}
                  </button>
                )}
                {!missingBinary && (
                  <button
                    className="secondary-button compact-btn"
                    disabled={providerRefreshBusy === p.provider_id}
                    title={toolOnly ? 'Verify provider configuration' : 'Probe provider and discover available models'}
                    onClick={() => void handleRefreshProvider(p)}
                  >
                    <RefreshCw
                      size={12}
                      className={providerRefreshBusy === p.provider_id ? 'spin' : ''}
                    />
                    {providerRefreshBusy === p.provider_id
                      ? 'Probing…'
                      : toolOnly
                        ? 'Verify'
                        : 'Discover Models'}
                  </button>
                )}
              </div>
            </article>
          );
        })}
      </div>

      {/* 2. Usage, Quotas & Spend Trend */}
      <div className="registry-heading">
        <div>
          <h2>Usage, quotas &amp; spend trend</h2>
          <p>Rolling-window subscription utilization, 7-day metered spend, and downshift recommendations</p>
        </div>
      </div>

      {usage ? (
        <div className="usage-grid">
          <div className="panel usage-card">
            <h3>Subscription quotas (3h / 24h)</h3>
            <div className="quota-list">
              {usage.subscriptions.map(sub => {
                const pct3h = Math.min(100, Math.round(sub.utilization_3h * 100));
                return (
                  <div className="quota-row" key={sub.provider}>
                    <div className="quota-meta">
                      <strong>{sub.provider}</strong>
                      <span>
                        3h: {sub.calls_3h}/{sub.soft_limit_3h} · 24h: {sub.calls_24h}/{sub.soft_limit_24h}
                      </span>
                    </div>
                    <div className="quota-bar-track">
                      <div
                        className={`quota-bar-fill ${pct3h > 85 ? 'warn' : ''}`}
                        style={{ width: `${pct3h}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="panel usage-card">
            <h3>Metered API spend</h3>
            <div className="spend-summary">
              <div>
                <span className="spend-label">TOTAL SPEND</span>
                <strong className="spend-value">${usage.metered.total_cost_usd.toFixed(4)}</strong>
              </div>
              <div>
                <span className="spend-label">7-DAY SPEND</span>
                <strong className="spend-value">${usage.metered.cost_7d_usd.toFixed(4)}</strong>
              </div>
              <div>
                <span className="spend-label">CALLS</span>
                <strong className="spend-value">{usage.metered.total_calls}</strong>
              </div>
            </div>
            <div className="trend-bars" aria-label="7-day spend trend">
              {usage.metered.daily_trend.map(day => {
                const heightPct = Math.max(8, Math.round((day.cost_usd / maxTrendCost) * 100));
                return (
                  <div
                    className="trend-col"
                    key={day.date}
                    title={`${day.date}: $${day.cost_usd.toFixed(4)} (${day.calls} calls)`}
                  >
                    <div className="trend-bar" style={{ height: `${heightPct}%` }} />
                    <span>{day.date.slice(5)}</span>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="panel usage-card">
            <h3>Downshift suggestions</h3>
            {usage.downshift_suggestions.length ? (
              <div className="downshift-list">
                {usage.downshift_suggestions.map(sug => (
                  <div className="downshift-row" key={sug.container_id}>
                    <div className="downshift-info">
                      <strong>
                        {sug.container_id}: {sug.current_model} → {sug.suggested_model}
                      </strong>
                      <span>
                        {sug.reason} (~{sug.estimated_savings_pct}% savings)
                      </span>
                    </div>
                    <button
                      className="primary-button compact-btn"
                      disabled={downshiftBusy === sug.container_id}
                      onClick={() =>
                        void handleDownshift(
                          sug.container_id,
                          sug.suggested_model,
                          sug.suggested_container_id,
                        )
                      }
                    >
                      <ArrowDownRight size={13} />
                      {downshiftBusy === sug.container_id ? 'Downshifting…' : 'Downshift Model'}
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <div className="empty-line">No downshift suggestions right now.</div>
            )}
          </div>
        </div>
      ) : (
        <div className="panel empty-line">Usage metrics will appear once events are recorded.</div>
      )}

      {/* 3. Compact Filterable Agent Rolodex */}
      <div className="registry-heading">
        <div>
          <h2>Agent rolodex</h2>
          <p>Searchable, filterable container roster with cost rank, model availability, and skills</p>
        </div>
        <div className="rolodex-filters">
          <div className="search-box">
            <Search size={14} />
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search id, model, skill…"
              aria-label="Search agent rolodex"
            />
          </div>
          <select
            value={providerFilter}
            onChange={e => setProviderFilter(e.target.value)}
            aria-label="Filter by provider"
          >
            <option value="all">All providers</option>
            {rolodexProviders.map(p => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <select
            value={tierFilter}
            onChange={e => setTierFilter(e.target.value)}
            aria-label="Filter by cost tier"
          >
            <option value="all">All tiers</option>
            {rolodexTiers.map(t => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="panel rolodex-table-wrap">
        {filteredRolodex.length ? (
          <table className="rolodex-table">
            <thead>
              <tr>
                <th>Container</th>
                <th>Provider</th>
                <th>Model</th>
                <th>Tier / Rank</th>
                <th>Skills</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {filteredRolodex.map(entry => {
                const serviceProvider = resolveServiceProvider(entry);
                const avail = getModelAvailability(serviceProvider, entry.model);
                const badgeClass = avail === 'tool_ready' ? 'ready' : avail;
                const dotClass =
                  avail === 'ready' || avail === 'tool_ready'
                    ? 'green'
                    : avail === 'unavailable'
                      ? 'yellow'
                      : 'muted';
                const badgeLabel =
                  avail === 'tool_ready'
                    ? 'Tool Ready'
                    : avail === 'ready'
                      ? 'Ready'
                      : avail === 'unavailable'
                        ? 'Unavailable'
                        : 'Unverified';
                return (
                  <tr key={entry.container_id}>
                    <td>
                      <strong>{entry.container_id}</strong>
                      <div className="rolodex-role">{entry.role}</div>
                    </td>
                    <td>{serviceProvider}</td>
                    <td>
                      <div className="rolodex-model-cell">
                        <span>{entry.model || '—'}</span>
                        {entry.model && (
                          <span
                            className={`model-avail-badge ${badgeClass}`}
                            title={
                              avail === 'tool_ready'
                                ? 'Search/tool provider configured and ready for agents'
                                : avail === 'ready'
                                  ? 'Model discovered & available for Agent Builder and Orchestrators'
                                  : avail === 'unavailable'
                                    ? 'Model is not currently available on this provider'
                                    : 'Provider models not yet verified'
                            }
                          >
                            <span className={`status-dot ${dotClass}`} />
                            {badgeLabel}
                          </span>
                        )}
                      </div>
                    </td>
                    <td>
                      {entry.cost_tier} (#{entry.cost_rank})
                    </td>
                    <td>
                      <div className="tags compact-tags">
                        {entry.skills.map(s => (
                          <span key={s}>{s}</span>
                        ))}
                      </div>
                    </td>
                    <td>
                      <button
                        type="button"
                        className="pill"
                        disabled={toggleBusy === entry.container_id}
                        onClick={() => void handleToggleActive(entry.container_id)}
                        title={entry.active ? 'Click to deactivate' : 'Click to activate'}
                      >
                        <span className={`status-dot ${entry.active ? 'green' : 'muted'}`} />
                        {toggleBusy === entry.container_id
                          ? 'Saving…'
                          : entry.active
                            ? 'Active'
                            : 'Inactive'}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <div className="empty-line">No containers match the current filters.</div>
        )}
      </div>

      {/* Preserved Agent Containers & Endpoint Capabilities */}
      <div className="registry-heading">
        <div>
          <h2>Agent containers</h2>
          <p>Configured agents and their specializations</p>
        </div>
      </div>
      <div className="agent-grid">
        {agents.length ? (
          agents.map(agent => {
            const serviceProvider = resolveServiceProvider(agent);
            const avail = getModelAvailability(serviceProvider, agent.model);
            const badgeClass = avail === 'tool_ready' ? 'ready' : avail;
            const dotClass =
              avail === 'ready' || avail === 'tool_ready'
                ? 'green'
                : avail === 'unavailable'
                  ? 'yellow'
                  : 'muted';
            const badgeLabel =
              avail === 'tool_ready'
                ? 'Tool Ready'
                : avail === 'ready'
                  ? 'Model Ready'
                  : avail === 'unavailable'
                    ? 'Model Unavailable'
                    : 'Unverified';
            return (
              <article className="panel agent-card" key={agent.container_id}>
                <div className="agent-card-top">
                  <div className="agent-icon">
                    <Bot size={20} />
                  </div>
                  <div className="agent-card-badges">
                    {agent.model && (
                      <span className={`model-avail-badge ${badgeClass}`}>
                        <span className={`status-dot ${dotClass}`} />
                        {badgeLabel}
                      </span>
                    )}
                    <span className="pill">
                      <span className={`status-dot ${agent.active ? 'green' : 'muted'}`} />{' '}
                      {agent.active ? 'Active' : 'Inactive'}
                    </span>
                  </div>
                </div>
                <h2>{agent.name}</h2>
                <div className="agent-provider">
                  {serviceProvider}
                  {agent.model ? ` · ${agent.model}` : ''}
                </div>
                <p>{agent.role}</p>
                <div className="tags">
                  {agent.strengths?.map(strength => (
                    <span key={strength}>{strength}</span>
                  ))}
                </div>
              </article>
            );
          })
        ) : (
          <div className="panel agents-empty">
            <Bot size={24} />
            <strong>No agents available</strong>
            <span>Configured agents will appear here.</span>
          </div>
        )}
      </div>

      <div className="registry-heading" ref={capabilitiesRef}>
        <div>
          <h2>Endpoint capabilities</h2>
          <p>
            {capabilityStats.available} available for Agent Builder &amp; Orchestrators ·{' '}
            {capabilityStats.unavailable} unavailable
          </p>
        </div>
        <div className="rolodex-filters">
          <div className="skills-filter-pills" role="group" aria-label="Filter models by availability">
            {(
              [
                { id: 'all', label: `All (${capabilities.length})` },
                { id: 'available', label: `Available (${capabilityStats.available})` },
                { id: 'unavailable', label: `Unavailable (${capabilityStats.unavailable})` },
              ] as const
            ).map(pill => (
              <button
                key={pill.id}
                type="button"
                className={`skills-filter-pill ${capAvailFilter === pill.id ? 'active' : ''}`}
                onClick={() => setCapAvailFilter(pill.id)}
              >
                {pill.label}
              </button>
            ))}
          </div>
          <select
            value={capProviderFilter}
            onChange={e => setCapProviderFilter(e.target.value)}
            aria-label="Filter capabilities by provider"
          >
            <option value="all">All providers</option>
            {capabilityStats.providers.map(p => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="agent-grid">
        {filteredCapabilities.length ? (
          filteredCapabilities.map(cap => {
            const isAvailable = cap.available ?? cap.healthy;
            const serviceProvider = cap.service_provider || cap.provider;
            const checkedLabel = formatTimestamp(cap.last_checked);
            return (
              <article
                className="panel agent-card"
                key={`${serviceProvider}:${cap.provider}:${cap.model_name}`}
              >
                <div className="agent-card-top">
                  <div className="agent-icon">
                    <Activity size={20} />
                  </div>
                  <span className="pill">
                    <span className={`status-dot ${isAvailable ? 'green' : 'red'}`} />{' '}
                    {isAvailable
                      ? cap.is_llm === false
                        ? 'Tool Ready'
                        : 'Builder / Orchestrator Ready'
                      : 'Unavailable'}
                  </span>
                </div>
                <h2>{cap.display_name || cap.model_name}</h2>
                {cap.display_name && cap.display_name !== cap.model_name && (
                  <div className="cap-model-id">
                    <code>{cap.model_name}</code>
                  </div>
                )}
                <div className="agent-provider">
                  {serviceProvider} · {cap.cost_tier}
                  {cap.max_context_tokens > 0 ? ` · ${cap.max_context_tokens.toLocaleString()} tokens` : ''}
                </div>
                <div className="cap-meta-row">
                  {cap.discovery_source && (
                    <span className={`discovery-badge ${cap.discovery_source}`}>
                      {cap.discovery_source}
                    </span>
                  )}
                  {cap.is_llm === false && <span className="discovery-badge tool">tool</span>}
                  {checkedLabel && <span className="cap-checked-time">Checked {checkedLabel}</span>}
                </div>
                <p>Recommended: {cap.recommended_roles?.join(', ') || 'None specified'}</p>
                <div className="tags">
                  {cap.strengths?.map(strength => (
                    <span key={strength}>{strength}</span>
                  ))}
                </div>
              </article>
            );
          })
        ) : (
          <div className="panel agents-empty">
            <Activity size={24} />
            <strong>
              {capabilities.length ? 'No models match the current filters' : 'No endpoints discovered'}
            </strong>
            <span>
              {capabilities.length
                ? 'Try resetting the provider or availability filter.'
                : 'Register a provider or refresh the registry to discover capabilities.'}
            </span>
          </div>
        )}
      </div>

      {/* Installed Skills (SkillForge & DAGGER) */}
      <div className="registry-heading">
        <div>
          <h2>Installed Skills (SkillForge &amp; DAGGER)</h2>
          <p>
            {skillCounts.total} total · {skillCounts.skillforge} SkillForge · {skillCounts.dagger_local} DAGGER Local
            {skillCounts.workspace > 0 ? ` · ${skillCounts.workspace} Workspace` : ''}
          </p>
        </div>
        <div className="skills-controls">
          <div className="skills-filter-pills" role="group" aria-label="Filter skills by source">
            {(
              [
                { id: 'all', label: `All (${skillCounts.total})` },
                { id: 'skillforge', label: `SkillForge (${skillCounts.skillforge})` },
                { id: 'dagger_local', label: `DAGGER Local (${skillCounts.dagger_local})` },
                ...(skillCounts.workspace > 0
                  ? [{ id: 'workspace' as const, label: `Workspace (${skillCounts.workspace})` }]
                  : []),
              ] as const
            ).map(pill => (
              <button
                key={pill.id}
                type="button"
                className={`skills-filter-pill ${skillSourceFilter === pill.id ? 'active' : ''}`}
                onClick={() => setSkillSourceFilter(pill.id)}
              >
                {pill.label}
              </button>
            ))}
          </div>
          <label className="search-box" aria-label="Search installed skills">
            <Search size={13} />
            <input
              value={skillSearch}
              onChange={e => setSkillSearch(e.target.value)}
              placeholder="Search skills, tags..."
            />
          </label>
        </div>
      </div>

      <div className="skills-grid">
        {filteredSkills.length > 0 ? (
          filteredSkills.map(skill => {
            const sourceLabel =
              skill.source === 'skillforge'
                ? 'SkillForge'
                : skill.source === 'dagger_local'
                  ? 'DAGGER Local'
                  : 'Workspace';
            const visibleTags = skill.tags.slice(0, 5);
            const extraTags = skill.tags.length - visibleTags.length;
            return (
              <article className="panel skill-card" key={`${skill.source}:${skill.path || skill.name}`}>
                <div className="skill-card-top">
                  <div className="skill-title-row">
                    <h3>{skill.name}</h3>
                    <span className={`skill-source-badge ${skill.source}`}>{sourceLabel}</span>
                  </div>
                  <span
                    className={`skill-valid-badge ${skill.valid ? 'valid' : 'invalid'}`}
                    title={
                      skill.valid
                        ? 'Skill manifest is valid'
                        : skill.validation_errors.join('; ') || 'Validation failed'
                    }
                  >
                    <span className={`status-dot ${skill.valid ? 'green' : 'red'}`} />
                    {skill.valid ? 'Valid' : 'Invalid'}
                  </span>
                </div>

                <p className="skill-description">
                  {skill.description || 'No description provided.'}
                </p>

                {!skill.valid && skill.validation_errors.length > 0 && (
                  <div className="skill-errors" role="note">
                    {skill.validation_errors.join(' · ')}
                  </div>
                )}

                {visibleTags.length > 0 && (
                  <div className="tags compact-tags">
                    {visibleTags.map(tag => (
                      <span key={tag}>{tag}</span>
                    ))}
                    {extraTags > 0 && <span className="skill-tag-more">+{extraTags}</span>}
                  </div>
                )}

                <div className="skill-meta-footer">
                  <div className="skill-locations" title={skill.locations.join(', ')}>
                    {skill.locations[0] || skill.path}
                    {skill.locations.length > 1 && ` (+${skill.locations.length - 1})`}
                  </div>
                  {skill.allowed_tools.length > 0 && (
                    <span
                      className="skill-tools-badge"
                      title={`Allowed tools: ${skill.allowed_tools.join(', ')}`}
                    >
                      {skill.allowed_tools.length} tools
                    </span>
                  )}
                </div>
              </article>
            );
          })
        ) : (
          <div className="panel agents-empty">
            <Bot size={28} />
            <strong>No installed skills match</strong>
            <span>
              {skills.length === 0
                ? 'No SkillForge or DAGGER local skills were discovered.'
                : 'Try clearing the search query or switching source filters.'}
            </span>
          </div>
        )}
      </div>

      {configModal && (
        <div
          className="modal-backdrop"
          onMouseDown={e => {
            if (e.target === e.currentTarget && !keyBusy) closeConfigModal();
          }}
        >
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="api-key-modal-title">
            <div className="modal-heading">
              <div>
                <h2 id="api-key-modal-title">Configure {configModal.displayName}</h2>
                <p>
                  {configModal.requiresApiKey
                    ? 'Saved locally to your 0o600 credentials vault and probed for available models.'
                    : 'Saved locally to your 0o600 credentials vault. Endpoint models will be discovered automatically.'}
                </p>
              </div>
              <button
                className="icon-button"
                aria-label="Close"
                onClick={closeConfigModal}
                disabled={keyBusy}
              >
                <X size={17} />
              </button>
            </div>
            <form onSubmit={e => void handleSaveConfig(e)}>
              {(configModal.supportsApiKey || configModal.requiresApiKey) && (
                <>
                  <label htmlFor="provider-api-key">
                    {configModal.requiresApiKey ? 'API Key' : 'API Key (optional)'}
                  </label>
                  <input
                    id="provider-api-key"
                    type="password"
                    value={apiKeyInput}
                    onChange={e => setApiKeyInput(e.target.value)}
                    placeholder={configModal.requiresApiKey ? 'sk-...' : 'Optional bearer token'}
                    required={configModal.requiresApiKey}
                    autoFocus={configModal.requiresApiKey}
                  />
                </>
              )}
              {configModal.supportsBaseUrl && (
                <>
                  <label htmlFor="provider-base-url">
                    {configModal.requiresApiKey
                      ? 'Base URL (optional — leave blank to clear)'
                      : 'Base URL (e.g. Ollama / LM Studio / vLLM)'}
                  </label>
                  <input
                    id="provider-base-url"
                    type="url"
                    value={baseUrlInput}
                    onChange={e => setBaseUrlInput(e.target.value)}
                    placeholder={
                      configModal.provider === 'local'
                        ? 'http://127.0.0.1:11434/v1'
                        : 'https://api.openai.com/v1'
                    }
                    autoFocus={!configModal.requiresApiKey}
                  />
                </>
              )}
              {modalError && (
                <div className="modal-error" role="alert">
                  {modalError}
                </div>
              )}
              <div className="modal-actions">
                <button
                  type="button"
                  className="secondary-button"
                  onClick={closeConfigModal}
                  disabled={keyBusy}
                >
                  Cancel
                </button>
                <button type="submit" className="primary-button" disabled={saveDisabled}>
                  {keyBusy ? 'Saving & discovering…' : 'Save & Discover Models'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </>
  );
}
