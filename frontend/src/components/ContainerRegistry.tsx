import { useMemo, useState } from 'react';
import {
  Activity, ArrowDownRight, Bot, KeyRound, LogIn, RefreshCw, Search, ShieldCheck, X,
} from 'lucide-react';
import { api } from '../api';
import type {
  Agent,
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
  onRefresh: () => void;
}

const STATUS_DOT: Record<string, string> = {
  authenticated: 'green',
  api_key_configured: 'green',
  expired: 'yellow',
  unconfigured: 'muted',
  missing_binary: 'red',
};

const STATUS_LABEL: Record<string, string> = {
  authenticated: 'Authenticated',
  api_key_configured: 'API Key Configured',
  expired: 'Expired',
  unconfigured: 'Unconfigured',
  missing_binary: 'Missing Binary',
};

export default function ContainerRegistry({
  agents,
  capabilities,
  providers,
  usage,
  rolodex,
  skills,
  skillSources = {},
  onRefresh,
}: ContainerRegistryProps) {
  const [loginBusy, setLoginBusy] = useState<string | null>(null);
  const [loginResult, setLoginResult] = useState<ProviderLoginResponse | null>(null);
  const [callbackInput, setCallbackInput] = useState('');
  const [completeBusy, setCompleteBusy] = useState(false);
  const [completeError, setCompleteError] = useState('');
  const [completeSuccess, setCompleteSuccess] = useState('');
  const [apiKeyModal, setApiKeyModal] = useState<{ provider: string; displayName: string } | null>(null);
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [baseUrlInput, setBaseUrlInput] = useState('');
  const [keyBusy, setKeyBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [downshiftBusy, setDownshiftBusy] = useState<string | null>(null);
  const [toggleBusy, setToggleBusy] = useState<string | null>(null);

  // Rolodex filters
  const [search, setSearch] = useState('');
  const [providerFilter, setProviderFilter] = useState('all');
  const [tierFilter, setTierFilter] = useState('all');

  // Skills filters
  const [skillSearch, setSkillSearch] = useState('');
  const [skillSourceFilter, setSkillSourceFilter] = useState<'all' | 'skillforge' | 'dagger_local' | 'workspace'>('all');

  const skillCounts = useMemo(() => {
    const counts = {
      total: skills.length,
      skillforge: skillSources.skillforge ?? skills.filter(s => s.source === 'skillforge').length,
      dagger_local: skillSources.dagger_local ?? skills.filter(s => s.source === 'dagger_local').length,
      workspace: skillSources.workspace ?? skills.filter(s => s.source === 'workspace').length,
    };
    return counts;
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

  const handleLogin = async (providerId: string) => {
    setLoginBusy(providerId);
    setLoginResult(null);
    setActionError('');
    setCallbackInput('');
    setCompleteError('');
    setCompleteSuccess('');
    try {
      const res = await api.providerLogin(providerId);
      setLoginResult(res);
      onRefresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Login request failed.');
    } finally {
      setLoginBusy(null);
    }
  };

  const handleCompleteLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!loginResult || !callbackInput.trim()) return;
    setCompleteBusy(true);
    setCompleteError('');
    setCompleteSuccess('');
    try {
      const updated = await api.completeProviderLogin(loginResult.provider_id, callbackInput.trim());
      if (updated.status === 'authenticated' || updated.status === 'api_key_configured') {
        setLoginResult(null);
        setCallbackInput('');
        setCompleteSuccess(`Signed in to ${updated.display_name || updated.provider_id}.`);
      } else {
        setCompleteError(updated.detail || `Sign-in did not complete (status: ${updated.status}).`);
      }
      onRefresh();
    } catch (err) {
      setCompleteError(err instanceof Error ? err.message : 'Failed to complete sign-in.');
    } finally {
      setCompleteBusy(false);
    }
  };

  const handleSaveKey = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!apiKeyModal || !apiKeyInput.trim()) return;
    setKeyBusy(true);
    setActionError('');
    try {
      await api.saveProviderCredentials(
        apiKeyModal.provider,
        apiKeyInput.trim(),
        apiKeyModal.provider === 'api' ? baseUrlInput.trim() : undefined,
      );
      setApiKeyModal(null);
      setApiKeyInput('');
      setBaseUrlInput('');
      onRefresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Could not save credentials.');
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
      onRefresh();
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
      onRefresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Could not update container status.');
    } finally {
      setToggleBusy(null);
    }
  };

  const rolodexProviders = useMemo(
    () => [...new Set(rolodex.map(r => r.provider))].sort(),
    [rolodex],
  );
  const rolodexTiers = useMemo(
    () => [...new Set(rolodex.map(r => r.cost_tier))].sort(),
    [rolodex],
  );

  const filteredRolodex = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rolodex.filter(entry => {
      if (providerFilter !== 'all' && entry.provider !== providerFilter) return false;
      if (tierFilter !== 'all' && entry.cost_tier !== tierFilter) return false;
      if (!q) return true;
      return (
        entry.container_id.toLowerCase().includes(q) ||
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
          <p>Live authentication status, CLI login, and metered API credentials</p>
        </div>
        <div className="registry-actions-row">
          <button
            className="secondary-button"
            onClick={() => setApiKeyModal({ provider: 'api', displayName: 'Custom OpenAI-Compatible API' })}
          >
            <KeyRound size={14} /> Custom API Key
          </button>
          <button className="secondary-button" onClick={onRefresh}>
            <RefreshCw size={14} /> Refresh registry
          </button>
        </div>
      </div>

      {completeSuccess && (
        <div className="panel login-banner">
          <div className="login-banner-body">
            <strong>Sign-in complete</strong>
            <span>{completeSuccess}</span>
          </div>
          <button className="icon-button" aria-label="Dismiss sign-in success" onClick={() => setCompleteSuccess('')}>
            <X size={15} />
          </button>
        </div>
      )}

      {loginResult && (() => {
        const authUrl = loginResult.auth_url || loginResult.device_url;
        return (
          <div className="panel login-banner">
            <div className="login-banner-body">
              <strong>
                {loginResult.provider_id} · {loginResult.status}
              </strong>
              <span>{loginResult.message}</span>
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
              {loginResult.status === 'awaiting_callback' && (
                <form className="callback-handoff-form" onSubmit={e => void handleCompleteLogin(e)}>
                  <label htmlFor="provider-callback-input">
                    Paste authorization code or localhost callback URL
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
                  </div>
                  {completeError && <div className="error-text">{completeError}</div>}
                </form>
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
        {providers.map(p => (
          <article className="panel provider-card" key={p.provider_id}>
            <div className="agent-card-top">
              <div className="provider-title-group">
                <ShieldCheck size={16} className="provider-shield" />
                <strong>{p.display_name}</strong>
              </div>
              <span className="pill">
                <span className={`status-dot ${STATUS_DOT[p.status] || 'muted'}`} />
                {STATUS_LABEL[p.status] || p.status}
              </span>
            </div>
            <div className="provider-detail">{p.detail || p.kind}</div>
            <div className="provider-card-actions">
              {p.supports_login && (
                <button
                  className="secondary-button compact-btn"
                  disabled={loginBusy === p.provider_id}
                  onClick={() => void handleLogin(p.provider_id)}
                >
                  <LogIn size={13} />
                  {loginBusy === p.provider_id ? 'Starting…' : 'Sign In / Re-Auth'}
                </button>
              )}
              {p.supports_api_key && (
                <button
                  className="secondary-button compact-btn"
                  onClick={() =>
                    setApiKeyModal({ provider: p.provider_id, displayName: p.display_name })
                  }
                >
                  <KeyRound size={13} />
                  {p.status === 'api_key_configured' ? 'Update Key' : 'Add API Key'}
                </button>
              )}
            </div>
          </article>
        ))}
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
          <p>Searchable, filterable container roster with cost rank and skills</p>
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
              {filteredRolodex.map(entry => (
                <tr key={entry.container_id}>
                  <td>
                    <strong>{entry.container_id}</strong>
                    <div className="rolodex-role">{entry.role}</div>
                  </td>
                  <td>{entry.provider}</td>
                  <td>{entry.model || '—'}</td>
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
              ))}
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
          agents.map(agent => (
            <article className="panel agent-card" key={agent.container_id}>
              <div className="agent-card-top">
                <div className="agent-icon">
                  <Bot size={20} />
                </div>
                <span className="pill">
                  <span className={`status-dot ${agent.active ? 'green' : 'muted'}`} />{' '}
                  {agent.active ? 'Active' : 'Inactive'}
                </span>
              </div>
              <h2>{agent.name}</h2>
              <div className="agent-provider">
                {agent.provider}
                {agent.model ? ` · ${agent.model}` : ''}
              </div>
              <p>{agent.role}</p>
              <div className="tags">
                {agent.strengths?.map(strength => (
                  <span key={strength}>{strength}</span>
                ))}
              </div>
            </article>
          ))
        ) : (
          <div className="panel agents-empty">
            <Bot size={24} />
            <strong>No agents available</strong>
            <span>Configured agents will appear here.</span>
          </div>
        )}
      </div>

      <div className="registry-heading">
        <div>
          <h2>Endpoint capabilities</h2>
          <p>Discovered providers and models</p>
        </div>
      </div>
      <div className="agent-grid">
        {capabilities.length ? (
          capabilities.map(cap => (
            <article className="panel agent-card" key={`${cap.provider}:${cap.model_name}`}>
              <div className="agent-card-top">
                <div className="agent-icon">
                  <Activity size={20} />
                </div>
                <span className="pill">
                  <span className={`status-dot ${cap.healthy ? 'green' : 'red'}`} />{' '}
                  {cap.healthy ? 'Healthy' : 'Unavailable'}
                </span>
              </div>
              <h2>{cap.model_name}</h2>
              <div className="agent-provider">
                {cap.provider} · {cap.cost_tier} · {cap.max_context_tokens.toLocaleString()} tokens
              </div>
              <p>Recommended: {cap.recommended_roles?.join(', ') || 'None specified'}</p>
              <div className="tags">
                {cap.strengths?.map(strength => (
                  <span key={strength}>{strength}</span>
                ))}
              </div>
            </article>
          ))
        ) : (
          <div className="panel agents-empty">
            <Activity size={24} />
            <strong>No endpoints discovered</strong>
            <span>Refresh the registry to discover capabilities.</span>
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

      {apiKeyModal && (
        <div
          className="modal-backdrop"
          onMouseDown={e => {
            if (e.target === e.currentTarget && !keyBusy) setApiKeyModal(null);
          }}
        >
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="api-key-modal-title">
            <div className="modal-heading">
              <div>
                <h2 id="api-key-modal-title">Configure {apiKeyModal.displayName}</h2>
                <p>Saved locally to your 0o600 credentials vault.</p>
              </div>
              <button
                className="icon-button"
                aria-label="Close"
                onClick={() => setApiKeyModal(null)}
                disabled={keyBusy}
              >
                <X size={17} />
              </button>
            </div>
            <form onSubmit={e => void handleSaveKey(e)}>
              <label htmlFor="provider-api-key">API Key</label>
              <input
                id="provider-api-key"
                type="password"
                value={apiKeyInput}
                onChange={e => setApiKeyInput(e.target.value)}
                placeholder="sk-..."
                required
                autoFocus
              />
              {apiKeyModal.provider === 'api' && (
                <>
                  <label htmlFor="provider-base-url">Base URL (optional)</label>
                  <input
                    id="provider-base-url"
                    type="url"
                    value={baseUrlInput}
                    onChange={e => setBaseUrlInput(e.target.value)}
                    placeholder="https://api.openai.com/v1"
                  />
                </>
              )}
              <div className="modal-actions">
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => setApiKeyModal(null)}
                  disabled={keyBusy}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="primary-button"
                  disabled={keyBusy || !apiKeyInput.trim()}
                >
                  {keyBusy ? 'Saving…' : 'Save & Refresh'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </>
  );
}
