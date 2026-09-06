import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

const MALAGA_CENTER = [36.7213, -4.4214];
const RISK_COLORS = { critico: "#b91c1c", alto: "#d97706", medio: "#0f4dbf", bajo: "#15803d" };

const DIMENSION_CHIPS = [
  { key: "afectaciones_urbanas", label: "Afectaciones Urbanas" },
  { key: "movilidad_trafico", label: "Movilidad · Tráfico" },
  { key: "movilidad_parking", label: "Movilidad · Parking" },
  { key: "control_gestion_its", label: "Control ITS" },
  { key: "movilidad_parking_y_trafico", label: "Cruce · Parking × Tráfico" },
  { key: "afectaciones_its_y_trafico", label: "Cruce · Afectaciones × ITS" },
  { key: "ocupacion_afectaciones_y_pmr", label: "Cruce · Ocupación × Afectaciones" },
  { key: "ocupacion_carga_y_trafico", label: "Cruce · Ocupación × Carga/Descarga" },
  { key: "afectaciones_trafico_impacto", label: "Cruce · Afectaciones × Tráfico" },
];

const DATASET_OPTIONS = [
  { value: "afectaciones_urbanas", label: "Afectaciones urbanas", url: "/ingesta/afectaciones" },
  { value: "movilidad_trafico", label: "Movilidad · Tráfico", url: "/ingesta/capa1/preservar?dataset=movilidad_trafico" },
  { value: "movilidad_parking", label: "Movilidad · Parking", url: "/ingesta/capa1/preservar?dataset=movilidad_parking" },
  { value: "movilidad_plazas_reservadas", label: "Movilidad · Plazas reservadas", url: "/ingesta/capa1/preservar?dataset=movilidad_plazas_reservadas" },
  { value: "movilidad_carriles_bici", label: "Movilidad · Carriles bici", url: "/ingesta/capa1/preservar?dataset=movilidad_carriles_bici" },
  { value: "control_gestion_its", label: "Control y Gestión ITS", url: "/ingesta/capa1/preservar?dataset=control_gestion_its" },
  { value: "ocupacion_permanente_espacio_publico", label: "Ocupación permanente", url: "/ingesta/capa1/preservar?dataset=ocupacion_permanente_espacio_publico" },
];

const PERIODOS = ["Anual", "Trimestre 1", "Trimestre 2", "Trimestre 3", "Trimestre 4", "Mensual"];

const FRANJAS_HORARIAS = ["00:00-06:00", "06:00-09:00", "09:00-13:00", "13:00-16:00", "16:00-20:00", "20:00-24:00"];
const DIAS_SEMANA = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"];
const TIPOS_VEHICULO = ["general", "moto", "ligero", "pesado"];

function riskClassFromPct(pct) {
  const value = Number(pct || 0);
  if (value >= 50) return "risk-critico";
  if (value >= 30) return "risk-alto";
  if (value >= 15) return "risk-medio";
  return "risk-bajo";
}

function riskClassFromNivel(nivel) {
  const key = normalizeMunicipio(nivel);
  if (key === "critico") return "risk-critico";
  if (key === "alto") return "risk-alto";
  if (key === "medio") return "risk-medio";
  return "risk-bajo";
}

function safeParse(value, fallback = null) {
  if (!value) return fallback;
  try {
    return JSON.parse(value) ?? fallback;
  } catch (error) {
    return fallback;
  }
}

function normalizeMunicipio(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .trim()
    .toLowerCase();
}

function formatNumber(value, suffix = "") {
  const numeric = Number(value || 0);
  const formatted = Number.isInteger(numeric) ? numeric.toLocaleString("es-ES") : numeric.toLocaleString("es-ES", { maximumFractionDigits: 1 });
  return `${formatted}${suffix}`;
}

function renderLiteBold(line, lineKey) {
  const parts = line.split(/\*\*(.+?)\*\*/g);
  return (
    <p className="q-answer-line" key={lineKey}>
      {parts.map((part, index) => (index % 2 === 1 ? <strong key={index}>{part}</strong> : part))}
    </p>
  );
}

function AnswerText({ text }) {
  const lines = (text || "").split("\n").filter((line) => line.trim().length > 0);
  return <>{lines.map((line, index) => renderLiteBold(line, index))}</>;
}

function BarChart({ title, items, labelKey, suffix = "" }) {
  const maximum = Math.max(...items.map((item) => Number(item.count || 0)), 1);
  return (
    <article className="chart-card">
      <h2>{title}</h2>
      {items.length === 0 ? <p className="chart-empty">No hay datos disponibles todavía.</p> : (
        <div className="bar-list">
          {items.slice(0, 8).map((item) => {
            const value = Number(item.count || 0);
            return (
              <div className="bar-row" key={`${item[labelKey]}-${value}`}>
                <div className="bar-label"><span>{item[labelKey] || "Sin dato"}</span><strong>{formatNumber(value, suffix)}</strong></div>
                <div className="bar-track"><div className="bar-fill" style={{ width: `${(value / maximum) * 100}%` }} /></div>
              </div>
            );
          })}
        </div>
      )}
    </article>
  );
}

function KpiCard({ label, value, detail, accent = "blue" }) {
  return <article className={`kpi-card accent-${accent}`}><span>{label}</span><strong>{value}</strong><small>{detail}</small></article>;
}

function CityMap({ points }) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const layerRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = L.map(containerRef.current, { scrollWheelZoom: false }).setView(MALAGA_CENTER, 13);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19,
    }).addTo(map);
    layerRef.current = L.layerGroup().addTo(map);
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    const layer = layerRef.current;
    if (!map || !layer) return;
    layer.clearLayers();

    const valid = (points || [])
      .map((point) => ({ ...point, lat: Number(point.lat), lon: Number(point.lon) }))
      .filter((point) => Number.isFinite(point.lat) && Number.isFinite(point.lon));

    if (valid.length === 0) {
      map.setView(MALAGA_CENTER, 13);
      return;
    }

    valid.forEach((point) => {
      L.circleMarker([point.lat, point.lon], {
        radius: 9,
        color: "#ffffff",
        weight: 2,
        fillColor: RISK_COLORS[point.nivel] || RISK_COLORS.bajo,
        fillOpacity: 0.9,
      })
        .bindPopup(`<strong>${point.via}</strong><br />${(point.reasons || []).join("<br />")}`)
        .addTo(layer);
    });

    map.fitBounds(L.latLngBounds(valid.map((point) => [point.lat, point.lon])), { padding: [30, 30], maxZoom: 16 });
  }, [points]);

  return <div className="city-map" ref={containerRef} />;
}

export default function App() {
  const [user, setUser] = useState(() => safeParse(localStorage.getItem("municipal_user"), null));
  const [token, setToken] = useState(() => localStorage.getItem("municipal_token") || sessionStorage.getItem("municipal_token") || "");
  const [activeTab, setActiveTab] = useState("dimensiones");
  const [dashboardView, setDashboardView] = useState("global");
  const [dashboard, setDashboard] = useState({
    kpis: {},
    summary: { top_vias: [], por_tipo_afectacion: [], por_hora: [] },
    mobility: { trafico_por_via: [], parking_ocupacion: [], plazas_por_tipo: [] },
    its: { por_categoria: [] },
    occupancy: { por_tipo: [], superficie_por_via: [] },
    priority_zones: [],
  });
  const [congestion, setCongestion] = useState({ loading: false, error: "", data: null });
  const [prediccionForm, setPrediccionForm] = useState({ direccion: "", franja_horaria: "", dia_semana: "", trafico_vehiculo: "general" });
  const [prediccion, setPrediccion] = useState({ loading: false, error: "", data: null });
  const [prediccionMl, setPrediccionMl] = useState({ loading: false, error: "", data: null });
  const [statusText, setStatusText] = useState("Comprobando backend...");
  const [statusError, setStatusError] = useState(false);
  const [resultText, setResultText] = useState("Esperando carga...");
  const [rows, setRows] = useState([]);
  const [lastFileName, setLastFileName] = useState("Sin archivo");
  const [questions, setQuestions] = useState([]);
  const [activeDimension, setActiveDimension] = useState("afectaciones_urbanas");
  const [openQuestion, setOpenQuestion] = useState(null);
  const [selectedFile, setSelectedFile] = useState(null);
  const [uploadStep, setUploadStep] = useState("select");
  const [uploadForm, setUploadForm] = useState({ dataset: "", anio: new Date().getFullYear(), periodo: "Anual", descripcion: "", visibilidad: "compartido" });
  const [uploadResult, setUploadResult] = useState(null);
  const [dragActive, setDragActive] = useState(false);
  const [authForm, setAuthForm] = useState({ email: "", password: "" });
  const [authError, setAuthError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [layer4Summary, setLayer4Summary] = useState({
    is_valid: true,
    kpis: { total_afectaciones: 0 },
    summary: { top_vias: [], por_tipo_afectacion: [], por_hora: [] },
  });
  const [adminSubTab, setAdminSubTab] = useState("usuarios");
  const [adminUsuarios, setAdminUsuarios] = useState([]);
  const [adminIngestas, setAdminIngestas] = useState([]);
  const [adminCatalogo, setAdminCatalogo] = useState([]);
  const [adminCatalogoHistorial, setAdminCatalogoHistorial] = useState([]);
  const [adminAceptaciones, setAdminAceptaciones] = useState([]);
  const [adminContratos, setAdminContratos] = useState([]);
  const [customDatasets, setCustomDatasets] = useState([]);
  const [nuevoContratoForm, setNuevoContratoForm] = useState({
    dataset_id: "",
    dimension: "",
    display_name: "",
    columnas: [{ nombre: "", tipo: "texto", obligatoria: false, valores_permitidos: "" }],
  });
  const [ingestaDetalle, setIngestaDetalle] = useState(null);
  const [catalogoEntregas, setCatalogoEntregas] = useState([]);
  const [catalogoStatus, setCatalogoStatus] = useState({ message: "", type: "success" });
  const [adminPolicyForm, setAdminPolicyForm] = useState({ version: "", titulo: "", contenido: "" });
  const [newUserForm, setNewUserForm] = useState({ name: "", email: "", password: "", municipio_id: "", role: "editor_municipio" });
  const [adminStatus, setAdminStatus] = useState({ message: "", type: "success" });
  const [policyModal, setPolicyModal] = useState({ open: false, version: "", titulo: "", contenido: "" });
  const pendingPolicyActionRef = useRef(null);
  const canUpload = user?.role === "admin_estatal" || user?.role === "editor_municipio";
  const datasetOptions = [
    ...DATASET_OPTIONS,
    ...customDatasets.map((c) => ({
      value: c.dataset_id,
      label: c.display_name,
      url: `/ingesta/capa1/preservar?dataset=${c.dataset_id}`,
    })),
  ];
  const isAdmin = user?.role === "admin_estatal";
  const canSeeAdminPanel = isAdmin || user?.role === "auditor";
  const userMunicipio = user?.municipio_id || "";
  const visibleRows = rows.filter((row) => {
    const rowMunicipio = [row.municipio_id, row.municipio, row.ayuntamiento, row.entidad, row.municipio_nombre]
      .find((value) => value !== undefined && value !== null && String(value).trim() !== "");

    if (!userMunicipio || !rowMunicipio) {
      return true;
    }

    const rowValue = normalizeMunicipio(rowMunicipio);
    const targetValue = normalizeMunicipio(userMunicipio);
    return rowValue.includes(targetValue) || targetValue.includes(rowValue);
  });

  useEffect(() => {
    if (!token || !user) {
      return;
    }

    void initializeSession();
  }, [token, user]);

  useEffect(() => {
    if (!token || !user) {
      return;
    }

    void loadQuestionsByDimension(activeDimension);
  }, [activeDimension, token, user]);

  useEffect(() => {
    if (!token || !user || activeTab !== "administracion" || !canSeeAdminPanel) {
      return;
    }

    void loadAdminPanel();
  }, [activeTab, token, user]);

  useEffect(() => {
    if (!token || !user || activeTab !== "catalogo-datos") {
      return;
    }

    void loadCatalogoEntregas();
  }, [activeTab, token, user]);

  function setStatus(message, isError = false) {
    setStatusText(message);
    setStatusError(isError);
  }

  async function checkBackend() {
    try {
      const response = await fetch("/health");
      const data = await response.json();
      setStatus("Backend conectado: " + data.status, false);
    } catch (error) {
      setStatus("No se pudo contactar con el backend.", true);
    }
  }

  async function loadRecords() {
    try {
      const response = await fetch("/afectaciones", { headers: { Authorization: `Bearer ${token}` } });
      if (response.status === 401) return handleUnauthorized();
      const data = await response.json();
      setRows(data.rows || []);
    } catch (error) {
      setStatus("No se pudieron cargar los registros.", true);
    }
  }

  async function loadQuestionsByDimension(dimension) {
    try {
      const response = await fetch(`/analysis/preguntas?dimension=${encodeURIComponent(dimension)}`);
      const data = await response.json();
      setQuestions(data.questions || []);
    } catch (error) {
      setQuestions([]);
    }
  }

  async function loadLayer4Summary() {
    try {
      const response = await fetch("/analysis/layer4/afectaciones/resumen", { headers: { Authorization: `Bearer ${token}` } });
      if (response.status === 401) return handleUnauthorized();
      const data = await response.json();
      if (data && data.is_valid !== false) {
        setLayer4Summary({
          is_valid: true,
          kpis: data.kpis || { total_afectaciones: 0 },
          summary: data.summary || { top_vias: [], por_tipo_afectacion: [], por_hora: [] },
        });
      } else {
        setLayer4Summary({
          is_valid: false,
          kpis: { total_afectaciones: 0 },
          summary: { top_vias: [], por_tipo_afectacion: [], por_hora: [] },
        });
      }
    } catch (error) {
      setLayer4Summary({
        is_valid: false,
        kpis: { total_afectaciones: 0 },
        summary: { top_vias: [], por_tipo_afectacion: [], por_hora: [] },
      });
    }
  }

  async function loadDashboard() {
    try {
      const response = await fetch("/analysis/cuadro-mando", { headers: { Authorization: `Bearer ${token}` } });
      if (response.status === 401) return handleUnauthorized();
      const data = await response.json();
      if (response.ok && data) {
        setDashboard((current) => ({ ...current, ...data }));
      }
    } catch (error) {
      setDashboard((current) => ({ ...current, errors: ["No se pudo cargar el cuadro de mando."] }));
    }
  }

  async function loadCongestion() {
    setCongestion((current) => ({ ...current, loading: true, error: "" }));
    try {
      const response = await fetch("/api/ml/movilidad/reglas-congestion", { headers: { Authorization: `Bearer ${token}` } });
      if (response.status === 401) return handleUnauthorized();
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data?.detail?.[0] || data?.detail || "No se pudo calcular la congestión.");
      }
      setCongestion({ loading: false, error: "", data });
    } catch (error) {
      setCongestion({ loading: false, error: error.message || "No se pudo calcular la congestión.", data: null });
    }
  }

  async function adminFetch(url, options = {}) {
    const response = await fetch(url, { ...options, headers: { ...(options.headers || {}), Authorization: `Bearer ${token}` } });
    if (response.status === 401) {
      handleUnauthorized();
      throw new Error("Sesión caducada.");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data?.detail?.message || data?.detail || `Código HTTP ${response.status}.`);
    }
    return data;
  }

  async function loadCatalogoEntregas() {
    setCatalogoStatus({ message: "", type: "success" });
    try {
      const data = await adminFetch("/catalogo/entregas");
      setCatalogoEntregas(data.entregas || []);
    } catch (error) {
      setCatalogoStatus({ message: error.message || "No se pudo cargar el catálogo de datos.", type: "error" });
    }
  }

  async function handleDescargarEntrega(entrega) {
    try {
      const response = await fetch(`/catalogo/entregas/${entrega.id}/descargar`, { headers: { Authorization: `Bearer ${token}` } });
      if (response.status === 401) return handleUnauthorized();
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data?.detail || `No se pudo descargar (HTTP ${response.status}).`);
      }
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${entrega.dataset}_${entrega.municipio_id}_${entrega.id}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      setCatalogoStatus({ message: error.message || "No se pudo descargar el fichero.", type: "error" });
    }
  }

  async function loadAdminPanel() {
    setAdminStatus({ message: "", type: "success" });
    try {
      if (isAdmin) {
        const usuarios = await adminFetch("/admin/usuarios");
        setAdminUsuarios(usuarios.usuarios || []);
        const catalogo = await adminFetch("/admin/catalogo");
        setAdminCatalogo(catalogo.catalogo || []);
        const historial = await adminFetch("/admin/catalogo/historial");
        setAdminCatalogoHistorial(historial.historial || []);
        const contratos = await adminFetch("/admin/contratos");
        setAdminContratos(contratos.contratos || []);
      }
      const ingestas = await adminFetch("/admin/ingestas?limit=100");
      setAdminIngestas(ingestas.entregas || []);
      const aceptaciones = await adminFetch("/admin/politicas/aceptaciones");
      setAdminAceptaciones(aceptaciones.aceptaciones || []);
    } catch (error) {
      setAdminStatus({ message: error.message || "No se pudo cargar el panel de administración.", type: "error" });
    }
  }

  async function handleUpdateUsuario(userId, patch) {
    try {
      await adminFetch(`/admin/usuarios/${userId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
      setAdminStatus({ message: "Usuario actualizado.", type: "success" });
      await loadAdminPanel();
    } catch (error) {
      setAdminStatus({ message: error.message || "No se pudo actualizar el usuario.", type: "error" });
    }
  }

  async function handleCreateUsuario(event) {
    event.preventDefault();
    if (!newUserForm.name.trim() || !newUserForm.email.trim() || !newUserForm.password || !newUserForm.municipio_id.trim()) return;
    try {
      await adminFetch("/admin/usuarios", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...newUserForm, municipio_id: newUserForm.municipio_id.trim().toUpperCase() }),
      });
      setAdminStatus({ message: `Usuario ${newUserForm.email} creado.`, type: "success" });
      setNewUserForm({ name: "", email: "", password: "", municipio_id: "", role: "editor_municipio" });
      await loadAdminPanel();
    } catch (error) {
      setAdminStatus({ message: error.message || "No se pudo crear el usuario.", type: "error" });
    }
  }

  async function handleDeleteUsuario(userId, email, role) {
    const mensaje = role === "admin_estatal"
      ? `⚠️ ${email} es una cuenta de ADMINISTRADOR del espacio de datos. ¿Seguro que quieres borrarla? Esta acción no se puede deshacer.`
      : `¿Borrar la cuenta ${email}? Esta acción no se puede deshacer.`;
    if (!window.confirm(mensaje)) return;
    try {
      await adminFetch(`/admin/usuarios/${userId}`, { method: "DELETE" });
      setAdminStatus({ message: `Usuario ${email} borrado.`, type: "success" });
      await loadAdminPanel();
    } catch (error) {
      setAdminStatus({ message: error.message || "No se pudo borrar el usuario.", type: "error" });
    }
  }

  async function handleToggleCatalogo(datasetId, currentStatus) {
    const nextStatus = currentStatus === "active" ? "inactive" : "active";
    try {
      await adminFetch(`/admin/catalogo/${datasetId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: nextStatus }) });
      setAdminStatus({ message: `Dataset ${datasetId} marcado como ${nextStatus === "active" ? "activo" : "inactivo"}.`, type: "success" });
      await loadAdminPanel();
    } catch (error) {
      setAdminStatus({ message: error.message || "No se pudo actualizar el catálogo.", type: "error" });
    }
  }

  async function handleVerDetalleIngesta(deliveryId) {
    try {
      const detail = await adminFetch(`/admin/ingestas/${deliveryId}`);
      setIngestaDetalle(detail);
    } catch (error) {
      setAdminStatus({ message: error.message || "No se pudo cargar el detalle de la entrega.", type: "error" });
    }
  }

  async function handleBorrarIngesta(deliveryId, dataset, municipio) {
    const confirmado = window.confirm(
      `⚠️ Vas a borrar PERMANENTEMENTE la entrega #${deliveryId} (${dataset} · ${municipio}).\n\n` +
      "Esto elimina sus filas de lake.curated y todo su rastro de seguimiento. No se puede deshacer. " +
      "Pensado solo para deshacer entregas de prueba, no para producción.\n\n¿Continuar?"
    );
    if (!confirmado) return;
    try {
      const result = await adminFetch(`/admin/ingestas/${deliveryId}`, { method: "DELETE" });
      setAdminStatus({
        message: `Entrega #${deliveryId} borrada: ${result.curated_rows_deleted} filas eliminadas de lake.curated.${result.table}.`,
        type: "success",
      });
      setIngestaDetalle(null);
      await loadAdminPanel();
    } catch (error) {
      setAdminStatus({ message: error.message || "No se pudo borrar la entrega.", type: "error" });
    }
  }

  async function handleResincronizarCatalogo() {
    try {
      await adminFetch("/gobierno/contratos/sincronizar", { method: "POST" });
      setAdminStatus({ message: "Catálogo resincronizado.", type: "success" });
      await loadAdminPanel();
    } catch (error) {
      setAdminStatus({ message: error.message || "No se pudo resincronizar el catálogo.", type: "error" });
    }
  }

  function handleContratoColumnaChange(index, patch) {
    setNuevoContratoForm((current) => ({
      ...current,
      columnas: current.columnas.map((columna, i) => (i === index ? { ...columna, ...patch } : columna)),
    }));
  }

  function handleAddContratoColumna() {
    setNuevoContratoForm((current) => ({
      ...current,
      columnas: [...current.columnas, { nombre: "", tipo: "texto", obligatoria: false, valores_permitidos: "" }],
    }));
  }

  function handleRemoveContratoColumna(index) {
    setNuevoContratoForm((current) => ({
      ...current,
      columnas: current.columnas.filter((_, i) => i !== index),
    }));
  }

  async function handleCrearContrato(event) {
    event.preventDefault();
    const payload = {
      dataset_id: nuevoContratoForm.dataset_id.trim(),
      dimension: nuevoContratoForm.dimension.trim(),
      display_name: nuevoContratoForm.display_name.trim(),
      columnas: nuevoContratoForm.columnas
        .filter((columna) => columna.nombre.trim())
        .map((columna) => ({
          nombre: columna.nombre.trim(),
          tipo: columna.tipo,
          obligatoria: columna.obligatoria,
          valores_permitidos: columna.valores_permitidos
            ? columna.valores_permitidos.split(",").map((valor) => valor.trim()).filter(Boolean)
            : [],
        })),
    };
    try {
      const result = await adminFetch("/admin/contratos", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setAdminStatus({ message: `Contrato '${result.dataset_id}' creado. Ya está disponible para subir datos.`, type: "success" });
      setNuevoContratoForm({
        dataset_id: "",
        dimension: "",
        display_name: "",
        columnas: [{ nombre: "", tipo: "texto", obligatoria: false, valores_permitidos: "" }],
      });
      await loadAdminPanel();
      await loadCustomDatasets();
    } catch (error) {
      setAdminStatus({ message: error.message || "No se pudo crear el contrato.", type: "error" });
    }
  }

  async function handlePublishPolicy(event) {
    event.preventDefault();
    if (!adminPolicyForm.version.trim() || !adminPolicyForm.titulo.trim() || !adminPolicyForm.contenido.trim()) return;
    try {
      await adminFetch("/admin/politicas", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(adminPolicyForm) });
      setAdminStatus({ message: `Política ${adminPolicyForm.version} publicada. Los usuarios deberán volver a aceptarla.`, type: "success" });
      setAdminPolicyForm({ version: "", titulo: "", contenido: "" });
      await loadAdminPanel();
    } catch (error) {
      setAdminStatus({ message: error.message || "No se pudo publicar la política.", type: "error" });
    }
  }

  async function ensurePolicyAccepted(onAccepted) {
    let response;
    let policy;
    try {
      response = await fetch("/politicas/vigente", { headers: { Authorization: `Bearer ${token}` } });
      if (response.status === 401) {
        handleUnauthorized();
        return false;
      }
      policy = await response.json();
    } catch (error) {
      setStatus("No se pudo comprobar las condiciones de uso vigentes. Inténtalo de nuevo.", true);
      return false;
    }
    if (!response.ok || !policy.version) {
      setStatus(policy?.detail || "No se pudo comprobar las condiciones de uso vigentes. Inténtalo de nuevo.", true);
      return false;
    }
    if (!policy.ya_aceptada) {
      pendingPolicyActionRef.current = onAccepted || null;
      setPolicyModal({ open: true, version: policy.version, titulo: policy.titulo, contenido: policy.contenido });
      return false;
    }
    return true;
  }

  async function handleAcceptPolicy() {
    const response = await fetch("/politicas/aceptar", { method: "POST", headers: { Authorization: `Bearer ${token}` } });
    if (response.status === 401) return handleUnauthorized();
    setPolicyModal({ open: false, version: "", titulo: "", contenido: "" });
    const pending = pendingPolicyActionRef.current;
    pendingPolicyActionRef.current = null;
    if (pending) await pending();
  }

  function handleCancelPolicy() {
    pendingPolicyActionRef.current = null;
    setPolicyModal({ open: false, version: "", titulo: "", contenido: "" });
    setStatus("No puedes subir ni consultar datos hasta aceptar las condiciones de uso vigentes.", true);
  }

  async function loadCustomDatasets() {
    try {
      const response = await fetch("/contratos/disponibles", { headers: { Authorization: `Bearer ${token}` } });
      if (response.status === 401) return handleUnauthorized();
      const data = await response.json();
      setCustomDatasets(data.personalizados || []);
    } catch (error) {
      setCustomDatasets([]);
    }
  }

  async function initializeSession() {
    const accepted = await ensurePolicyAccepted(() => initializeSession());
    if (!accepted) return; // el modal reintentará esta misma función tras aceptar

    void checkBackend();
    void loadRecords();
    void loadDashboard();
    void loadCongestion();
    void loadCustomDatasets();
  }

  async function submitPrediccion(event) {
    event.preventDefault();
    if (!prediccionForm.direccion.trim()) return;
    setPrediccion({ loading: true, error: "", data: null });
    try {
      const response = await fetch("/api/ml/movilidad/predecir", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          direccion: prediccionForm.direccion.trim(),
          franja_horaria: prediccionForm.franja_horaria || null,
          dia_semana: prediccionForm.dia_semana || null,
        }),
      });
      if (response.status === 401) return handleUnauthorized();
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data?.detail?.[0] || data?.detail || "Sin histórico suficiente para esa vía.");
      }
      setPrediccion({ loading: false, error: "", data });
    } catch (error) {
      setPrediccion({ loading: false, error: error.message || "No se pudo calcular la predicción.", data: null });
    }

    if (!prediccionForm.dia_semana || !prediccionForm.franja_horaria) {
      setPrediccionMl({ loading: false, error: "Rellena día de la semana y franja horaria para consultar el modelo entrenado.", data: null });
      return;
    }
    setPrediccionMl({ loading: true, error: "", data: null });
    try {
      const response = await fetch("/api/ml/movilidad/predecir-ml", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          direccion: prediccionForm.direccion.trim(),
          franja_horaria: prediccionForm.franja_horaria,
          dia_semana: prediccionForm.dia_semana,
          trafico_vehiculo: prediccionForm.trafico_vehiculo || "general",
        }),
      });
      if (response.status === 401) return handleUnauthorized();
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data?.detail?.[0] || data?.detail || "El modelo no pudo predecir.");
      }
      setPrediccionMl({ loading: false, error: "", data });
    } catch (error) {
      setPrediccionMl({ loading: false, error: error.message || "No se pudo consultar el modelo.", data: null });
    }
  }

  async function handleAuthSubmit(event) {
    event.preventDefault();
    setAuthError("");
    setSubmitting(true);

    try {
      const email = authForm.email.trim();
      const password = authForm.password;

      if (!email || !password) {
        throw new Error("Email y contraseña son obligatorios.");
      }

      const response = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || "Credenciales no válidas.");
      }
      const authenticatedUser = data.user || { id: data.id, name: data.name, email: data.email, municipio_id: data.municipio_id };
      const newToken = data.token;

      localStorage.setItem("municipal_token", newToken);
      localStorage.setItem("municipal_user", JSON.stringify(authenticatedUser));
      sessionStorage.setItem("municipal_token", newToken);

      setToken(newToken);
      setUser(authenticatedUser);
      setAuthForm({ email: "", password: "" });
      setAuthError("");
      setStatus("Sesión iniciada correctamente.", false);
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "No se pudo iniciar sesión.");
    } finally {
      setSubmitting(false);
    }
  }

  function handleLogout(message) {
    localStorage.removeItem("municipal_token");
    localStorage.removeItem("municipal_user");
    sessionStorage.removeItem("municipal_token");
    setToken("");
    setUser(null);
    setAuthForm({ email: "", password: "" });
    setAuthError(message || "");
    setStatus("Comprobando backend...", false);
    setRows([]);
    setQuestions([]);
    resetUpload();
  }

  // Un token caducado/inválido siempre se traduce en 401: en vez de mostrar
  // "token no válido" como si fuera un error de la acción en curso, se cierra
  // la sesión y se explica por qué en la propia pantalla de login.
  function handleUnauthorized() {
    handleLogout("Tu sesión ha caducado. Vuelve a iniciar sesión.");
  }

  async function detectDatasetKey(file) {
    let headers = [];
    try {
      const firstLine = (await file.slice(0, 8192).text()).split(/\r?\n/, 1)[0];
      headers = firstLine.split(/[;,\t|]/).map((header) => normalizeHeader(header));
    } catch (error) {
      headers = [];
    }

    const has = (column) => headers.includes(column);
    if (has("tipo_plaza")) return "movilidad_plazas_reservadas";
    if (has("carril_bici_longitud")) return "movilidad_carriles_bici";
    if (has("trafico_flujo")) return "movilidad_trafico";
    if (has("ocupacion_libres")) return "movilidad_parking";
    if (has("categoria")) return "control_gestion_its";
    if (has("tipo_ocupacion") && has("estado_autorizacion")) return "ocupacion_permanente_espacio_publico";

    const normalizedName = file.name.toLowerCase();
    const byName = DATASET_OPTIONS.find((option) => normalizedName.includes(option.value));
    return byName?.value || "afectaciones_urbanas";
  }

  async function handleFileChosen(file) {
    if (!file) return;
    const detected = await detectDatasetKey(file);
    setSelectedFile(file);
    setUploadForm((current) => ({ ...current, dataset: detected }));
    setUploadResult(null);
    setUploadStep("classify");
  }

  function handleFileDrop(event) {
    event.preventDefault();
    setDragActive(false);
    const file = event.dataTransfer.files?.[0];
    if (file) void handleFileChosen(file);
  }

  function backUploadStep() {
    if (uploadStep === "classify") {
      setSelectedFile(null);
      setUploadStep("select");
      return;
    }
    setUploadStep("classify");
  }

  function resetUpload() {
    setSelectedFile(null);
    setUploadForm({ dataset: "", anio: new Date().getFullYear(), periodo: "Anual", descripcion: "", visibilidad: "compartido" });
    setUploadResult(null);
    setDragActive(false);
    setUploadStep("select");
  }

  async function confirmUpload() {
    if (!selectedFile || !uploadForm.dataset) return;

    const accepted = await ensurePolicyAccepted(() => confirmUpload());
    if (!accepted) return; // se reintenta automáticamente tras aceptar en el modal

    setUploadStep("uploading");
    setStatus("Subiendo archivo...", false);

    const target = datasetOptions.find((option) => option.value === uploadForm.dataset) || datasetOptions[0];
    const separator = target.url.includes("?") ? "&" : "?";
    const targetUrl = `${target.url}${separator}municipio_id=${encodeURIComponent(user?.municipio_id || "")}&anio=${encodeURIComponent(uploadForm.anio)}&period=${encodeURIComponent(uploadForm.periodo)}&visibilidad=${encodeURIComponent(uploadForm.visibilidad)}`;
    const formData = new FormData();
    formData.append("file", selectedFile);

    try {
      const response = await fetch(targetUrl, { method: "POST", body: formData, headers: { Authorization: `Bearer ${token}` } });
      if (response.status === 401) return handleUnauthorized();
      const responseText = await response.text();
      let result;
      try {
        result = responseText ? JSON.parse(responseText) : {};
      } catch (parseError) {
        result = { errors: [`El backend devolvió una respuesta no válida (${response.status}).`, responseText] };
      }

      setResultText(JSON.stringify(result, null, 2));
      setLastFileName(selectedFile.name || "archivo");

      if (response.ok && result.is_valid !== false) {
        const records = result.row_count ?? result.summary?.rows_received ?? result.inserted ?? result.summary?.rows_inserted ?? 0;
        const isDuplicate = result.status === "duplicate";
        const message = isDuplicate
          ? `El archivo ${selectedFile.name} ya estaba cargado para ${target.label}. No se ha duplicado.`
          : `Se ha recibido ${selectedFile.name} en ${target.label}. Registros procesados: ${records}.`;
        setUploadResult({ ok: true, message, isDuplicate });
        setStatus("Carga completada correctamente.", false);
        setUploadStep("done");
        await Promise.all([loadRecords(), loadDashboard(), loadCongestion()]);
        return;
      }

      const details = result.errors?.join(" ") || result.detail || `Código HTTP ${response.status}.`;
      setUploadResult({ ok: false, message: `No se ha podido incorporar el archivo: ${details}` });
      setStatus(`La carga falló: ${details}`, true);
      setUploadStep("error");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Error desconocido";
      setResultText(`No se pudo completar la carga.\n${message}`);
      setUploadResult({ ok: false, message: `No se pudo completar la carga: ${message}` });
      setStatus(`No se pudo completar la carga: ${message}`, true);
      setUploadStep("error");
    }
  }

  async function onUpload() {
    if (!selectedFile) {
      alert("Selecciona un archivo CSV");
      return;
    }

    setResultText("Subiendo archivo...");
    setStatus("Subiendo archivo...", false);

    const formData = new FormData();
    formData.append("file", selectedFile);
    const uploadTarget = await getUploadTarget(selectedFile);

    try {
      const response = await fetch(uploadTarget.url, {
        method: "POST",
        body: formData,
      });
      const responseText = await response.text();
      let result;
      try {
        result = responseText ? JSON.parse(responseText) : {};
      } catch (parseError) {
        result = { errors: [`El backend devolvió una respuesta no válida (${response.status}).`, responseText] };
      }
      setResultText(JSON.stringify(result, null, 2));
      if (response.ok && ["success", "accepted", "duplicate", "preserved", "received"].includes(result.status)) {
        const message = uploadTarget.layer1
          ? "Archivo recibido correctamente."
          : `Carga completada: ${result.summary?.rows_inserted || 0} filas insertadas.`;
        setStatus(message, false);
      } else {
        const details = result.errors?.join(" ") || result.detail || `Código HTTP ${response.status}.`;
        setStatus(`La carga falló: ${details}`, true);
      }
      setLastFileName(selectedFile.name || "archivo");
      if (response.ok && result.is_valid !== false) {
        await Promise.all([loadRecords(), loadLayer4Summary()]);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Error desconocido";
      setResultText(`No se pudo completar la carga.\n${message}`);
      setStatus(`No se pudo completar la carga: ${message}`, true);
    }
  }

  async function getUploadTarget(file) {
    const normalizedName = file.name.toLowerCase();
    let headers = [];
    try {
      const firstLine = (await file.slice(0, 8192).text()).split(/\r?\n/, 1)[0];
      headers = firstLine.split(/[;,\t|]/).map((header) => normalizeHeader(header));
    } catch (error) {
      headers = [];
    }

    const has = (column) => headers.includes(column);
    if (has("tipo_plaza")) {
      return { url: "/ingesta/capa1/preservar?dataset=movilidad_plazas_reservadas", layer1: true };
    }
    if (has("carril_bici_longitud")) {
      return { url: "/ingesta/capa1/preservar?dataset=movilidad_carriles_bici", layer1: true };
    }
    if (has("trafico_flujo")) {
      return { url: "/ingesta/capa1/preservar?dataset=movilidad_trafico", layer1: true };
    }
    if (has("ocupacion_libres")) {
      return { url: "/ingesta/capa1/preservar?dataset=movilidad_parking", layer1: true };
    }
    if (has("categoria")) {
      return { url: "/ingesta/capa1/preservar?dataset=control_gestion_its", layer1: true };
    }
    if (has("tipo_ocupacion") && has("estado_autorizacion")) {
      return { url: "/ingesta/capa1/preservar?dataset=ocupacion_permanente_espacio_publico", layer1: true };
    }

    const datasets = [
      "movilidad_carriles_bici",
      "movilidad_trafico",
      "movilidad_plazas_reservadas",
      "movilidad_parking",
    ];
    const mobilityDataset = datasets.find((dataset) => normalizedName.includes(dataset));
    if (mobilityDataset) {
      return { url: `/ingesta/capa1/preservar?dataset=${encodeURIComponent(mobilityDataset)}`, layer1: true };
    }
    if (normalizedName.includes("control_gestion_its")) {
      return { url: "/ingesta/capa1/preservar?dataset=control_gestion_its", layer1: true };
    }
    if (normalizedName.includes("ocupacion_permanente")) {
      return { url: "/ingesta/capa1/preservar?dataset=ocupacion_permanente_espacio_publico", layer1: true };
    }
    return { url: "/ingesta/afectaciones", layer1: false };
  }

  function normalizeHeader(value) {
    return value
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_|_$/g, "");
  }

  async function onPreserveLayer1() {
    if (!selectedFile) {
      alert("Selecciona un archivo CSV");
      return;
    }

    setResultText("Preservando fichero original...");
    setStatus("Preservando fichero original en Capa 1...", false);
    const formData = new FormData();
    formData.append("file", selectedFile);

    try {
      const response = await fetch("/ingesta/capa1/preservar?dataset=movilidad_trafico", {
        method: "POST",
        body: formData,
      });
      const responseText = await response.text();
      const result = responseText ? JSON.parse(responseText) : {};
      setResultText(JSON.stringify(result, null, 2));
      if (!response.ok || result.is_valid === false) {
        const details = result.errors?.join(" ") || result.detail || `Código HTTP ${response.status}.`;
        setStatus(`La preservación falló: ${details}`, true);
        return;
      }
      setStatus("Archivo recibido correctamente.", false);
      setLastFileName(selectedFile.name || "archivo");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Error desconocido";
      setResultText(`No se pudo preservar el fichero.\n${message}`);
      setStatus(`No se pudo preservar el fichero: ${message}`, true);
    }
  }

  async function resolveQuestionWithOllama(question) {
    setOpenQuestion({ id: question.id, loading: true, error: false, text: "", model: "" });
    try {
      const response = await fetch(`/analysis/interpretar/ollama/${encodeURIComponent(question.dimension)}?question_id=${encodeURIComponent(question.id)}`, { method: "POST" });
      const data = await response.json();
      if (!response.ok || data.is_valid === false) {
        const message = data.errors?.join(" ") || data.detail || `Código HTTP ${response.status}`;
        setOpenQuestion({ id: question.id, loading: false, error: true, text: `No se pudo generar la interpretación.\n${message}`, model: "" });
        return;
      }
      setOpenQuestion({
        id: question.id,
        loading: false,
        error: false,
        text: data.response || "Sin respuesta generada.",
        model: data.model || "",
      });
    } catch (error) {
      setOpenQuestion({ id: question.id, loading: false, error: true, text: "No se pudo contactar con el servicio de interpretación por IA.", model: "" });
    }
  }

  if (!token || !user) {
    return (
      <section className="auth-page">
        <div className="auth-intro">
          <div className="auth-intro-copy">
            <div className="auth-kicker">PLATAFORMA MUNICIPAL</div>
            <h1>Centro de Inteligencia Urbana</h1>
            <p>Gestiona incidencias, análisis y carga de datos con una visión filtrada por ayuntamiento.</p>
            <div className="auth-signal"><span />Acceso seguro por usuario y municipio</div>
          </div>
        </div>

        <div className="auth-panel">
          <h2>Bienvenido</h2>
          <p className="auth-subtitle">Accede con tu correo y contraseña para ver tu municipio.</p>

          <form onSubmit={handleAuthSubmit}>
            <label>
              Email
              <input
                type="email"
                value={authForm.email}
                onChange={(event) => setAuthForm((current) => ({ ...current, email: event.target.value }))}
                placeholder="usuario@ayuntamiento.es"
              />
            </label>

            <label>
              Contraseña
              <input
                type="password"
                value={authForm.password}
                onChange={(event) => setAuthForm((current) => ({ ...current, password: event.target.value }))}
                placeholder="••••••••"
              />
            </label>

            {authError && <p className="auth-error">{authError}</p>}

            <button type="submit" className="auth-submit" disabled={submitting}>
              <span>{submitting ? "…" : "→"}</span>
              {submitting ? "Accediendo..." : "Entrar"}
            </button>
          </form>

          <p className="auth-footnote">¿Necesitas una cuenta? Contacta con el administrador del espacio de datos.</p>
          <p className="auth-footnote"><a href="/catalogo/publico" target="_blank" rel="noreferrer">Consulta el catálogo abierto de datasets</a> sin necesidad de cuenta.</p>
        </div>
      </section>
    );
  }

  return (
    <>
      <header className="topbar">
        <button className="menu-btn" type="button">☰</button>
        <button className={`tab-btn ${activeTab === "dimensiones" ? "active" : ""}`} type="button" onClick={() => setActiveTab("dimensiones")}>Dimensiones</button>
        <button className={`tab-btn ${activeTab === "analisis" ? "active" : ""}`} type="button" onClick={() => setActiveTab("analisis")}>Análisis</button>
        <button className={`tab-btn ${activeTab === "cuadro-mando" ? "active" : ""}`} type="button" onClick={() => setActiveTab("cuadro-mando")}>Cuadro de mando</button>
        <button className={`tab-btn ${activeTab === "catalogo-datos" ? "active" : ""}`} type="button" onClick={() => setActiveTab("catalogo-datos")}>Catálogo de datos</button>
        {canSeeAdminPanel && (
          <button className={`tab-btn ${activeTab === "administracion" ? "active" : ""}`} type="button" onClick={() => setActiveTab("administracion")}>Administración</button>
        )}

        <div className="user-badge-container">
          <div className="user-info-box">
            <span className="user-info-name">👤 {user?.name}</span>
            <span className="user-municipio-tag">{user?.municipio_id}</span>
          </div>
          <button className="btn-logout" type="button" onClick={handleLogout}>Cerrar sesión</button>
        </div>
      </header>

      {statusText && statusError && (
        <div className="status-banner error" role="alert">{statusText}</div>
      )}

      <main className="page">
        {activeTab === "dimensiones" && (
          <section>
            <div className="hero">
              <h1>Centro de Inteligencia Urbana</h1>
              <p>Explora las dimensiones de datos activas y gestiona el ciclo de carga de información municipal.</p>
            </div>

            <h2 className="section-title">ECOSISTEMA DE DATOS ACTIVO</h2>

            <div className="dimension-grid">
              <article className="dim-card">
                <div className="icon icon-blue">⚡</div>
                <div>
                  <h3>Dimensión 1</h3>
                  <p>Movilidad</p>
                  <p>4 categorías</p>
                </div>
              </article>
              <article className="dim-card active">
                <div className="icon icon-amber">🚧</div>
                <div>
                  <h3>Dimensión 2</h3>
                  <p>Gestión de Afectaciones Urbanas</p>
                  <p>1 categoría activa</p>
                </div>
              </article>
              <article className="dim-card">
                <div className="icon icon-green">🏙</div>
                <div>
                  <h3>Dimensión 3</h3>
                  <p>Ocupación permanente del espacio público</p>
                  <p>Próximamente</p>
                </div>
              </article>
              <article className="dim-card">
                <div className="icon icon-indigo">🎥</div>
                <div>
                  <h3>Dimensión 4</h3>
                  <p>Control y Gestión ITS</p>
                  <p>Próximamente</p>
                </div>
              </article>
            </div>

            {!canUpload && (
              <div className="card mt-16">
                <div className="panel-heading"><h2>Carga de datos por dimensión</h2></div>
                <p className="chart-empty">Tu rol ({user?.role}) tiene acceso de solo lectura. Solo editor_municipio y admin_estatal pueden subir archivos.</p>
              </div>
            )}

            {canUpload && (
            <div className="card mt-16">
              <div className="upload-card-header">
                <div>
                  <h3 className="card-title">Carga de datos por dimensión</h3>
                  <p>Sigue los tres pasos para clasificar y confirmar tu archivo antes de incorporarlo.</p>
                </div>
                <span className="municipio-pill">{user?.municipio_id}</span>
              </div>

              <div className="upload-steps">
                <div className={`upload-step ${uploadStep === "select" ? "active" : ""}`}>1</div>
                <div className={`upload-step-line ${["classify", "summary", "uploading", "done", "error"].includes(uploadStep) ? "active" : ""}`} />
                <div className={`upload-step ${uploadStep === "classify" ? "active" : ""}`}>2</div>
                <div className={`upload-step-line ${["summary", "uploading", "done", "error"].includes(uploadStep) ? "active" : ""}`} />
                <div className={`upload-step ${["summary", "uploading", "done", "error"].includes(uploadStep) ? "active" : ""}`}>3</div>
              </div>

              <div className="upload-workflow">
                <div
                  className="upload-zone"
                  onDragOver={(event) => { if (uploadStep === "select") { event.preventDefault(); setDragActive(true); } }}
                  onDragLeave={() => setDragActive(false)}
                  onDrop={uploadStep === "select" ? handleFileDrop : undefined}
                >
                  {uploadStep === "select" && (
                    <>
                      <div className="upload-heading">
                        <div className="upload-icon">⬆</div>
                        <div>
                          <h4>Selecciona tu archivo</h4>
                          <p>Arrastra un CSV aquí o elígelo desde tu equipo.</p>
                        </div>
                      </div>
                      <label className="file-picker">
                        <span>{dragActive ? "Suelta el archivo aquí..." : "Ningún archivo elegido todavía"}</span>
                        <strong>Elegir archivo</strong>
                        <input className="file-input" type="file" accept=".csv" onChange={(event) => handleFileChosen(event.target.files?.[0] || null)} />
                      </label>
                      <p className="file-details">Formato admitido: .csv</p>
                    </>
                  )}

                  {uploadStep === "classify" && selectedFile && (
                    <>
                      <div className="upload-heading">
                        <div className="upload-icon">📄</div>
                        <div>
                          <h4>Clasifica la carga</h4>
                          <p>{selectedFile.name}</p>
                        </div>
                      </div>
                      <div className="upload-form-grid">
                        <label>
                          <span>Dimensión</span>
                          <select value={uploadForm.dataset} onChange={(event) => setUploadForm((current) => ({ ...current, dataset: event.target.value }))}>
                            {datasetOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                          </select>
                        </label>
                        <label>
                          <span>Año</span>
                          <input type="number" min="2020" max="2035" value={uploadForm.anio} onChange={(event) => setUploadForm((current) => ({ ...current, anio: event.target.value }))} />
                        </label>
                        <label>
                          <span>Periodo</span>
                          <select value={uploadForm.periodo} onChange={(event) => setUploadForm((current) => ({ ...current, periodo: event.target.value }))}>
                            {PERIODOS.map((periodo) => <option key={periodo} value={periodo}>{periodo}</option>)}
                          </select>
                        </label>
                        <label>
                          <span>Descripción (opcional)</span>
                          <input type="text" value={uploadForm.descripcion} onChange={(event) => setUploadForm((current) => ({ ...current, descripcion: event.target.value }))} placeholder="ej. Datos de tráfico trimestre 1" />
                        </label>
                        <label>
                          <span>Visibilidad</span>
                          <select value={uploadForm.visibilidad} onChange={(event) => setUploadForm((current) => ({ ...current, visibilidad: event.target.value }))}>
                            <option value="compartido">Compartido (visible para todo el espacio de datos)</option>
                            <option value="privado">Privado (solo tu municipio y administración)</option>
                          </select>
                        </label>
                      </div>
                      <div className="actions-row">
                        <button className="btn alt" type="button" onClick={backUploadStep}>Volver</button>
                        <button className="btn primary" type="button" onClick={() => setUploadStep("summary")}>Continuar</button>
                      </div>
                    </>
                  )}

                  {uploadStep === "summary" && selectedFile && (
                    <>
                      <div className="upload-heading">
                        <div className="upload-icon">✓</div>
                        <div>
                          <h4>Resumen antes de subir</h4>
                          <p>Revisa que todo sea correcto.</p>
                        </div>
                      </div>
                      <p className="file-details">El archivo se guardará tal cual, sin modificaciones, para preservar los datos originales.</p>
                      <div className="actions-row">
                        <button className="btn alt" type="button" onClick={backUploadStep}>Volver</button>
                        <button className="btn primary" type="button" onClick={confirmUpload}>Confirmar y subir</button>
                      </div>
                    </>
                  )}

                  {uploadStep === "uploading" && (
                    <div className="upload-heading">
                      <div className="upload-icon">…</div>
                      <div>
                        <h4>Subiendo archivo...</h4>
                        <p>Esto puede tardar unos segundos.</p>
                      </div>
                    </div>
                  )}

                  {(uploadStep === "done" || uploadStep === "error") && (
                    <>
                      <div className="upload-heading">
                        <div className="upload-icon">{uploadStep === "error" ? "!" : uploadResult?.isDuplicate ? "i" : "✓"}</div>
                        <div>
                          <h4>{uploadStep === "error" ? "No se ha incorporado" : uploadResult?.isDuplicate ? "Archivo ya registrado" : "Carga completada"}</h4>
                          <p className={uploadStep === "error" ? "error" : uploadResult?.isDuplicate ? "" : "success"}>{uploadResult?.message}</p>
                        </div>
                      </div>
                      <div className="actions-row">
                        <button className="btn primary" type="button" onClick={resetUpload}>Subir otro archivo</button>
                      </div>
                    </>
                  )}
                </div>

                <div className="upload-summary">
                  <h4>Resumen de la carga</h4>
                  <dl>
                    <div><dt>Ayuntamiento</dt><dd>{user?.municipio_id}</dd></div>
                    <div><dt>Dimensión</dt><dd>{datasetOptions.find((option) => option.value === uploadForm.dataset)?.label || "—"}</dd></div>
                    <div><dt>Periodo</dt><dd>{uploadForm.dataset ? `${uploadForm.periodo} ${uploadForm.anio}` : "—"}</dd></div>
                    <div><dt>Archivo</dt><dd>{selectedFile?.name || "—"}</dd></div>
                  </dl>
                </div>
              </div>
            </div>
            )}

          </section>
        )}

        {activeTab === "analisis" && (
          <section>
            <div className="hero">
              <h1>Análisis Inteligente</h1>
              <p>Preguntas analíticas organizadas por dimensión urbana.</p>
            </div>

            <div className="chip-row">
              {DIMENSION_CHIPS.map((chip) => (
                <button
                  key={chip.key}
                  className={`chip ${activeDimension === chip.key ? "active" : ""}`}
                  type="button"
                  onClick={() => setActiveDimension(chip.key)}
                >
                  {chip.label}
                </button>
              ))}
            </div>

            <div className="questions-grid">
              {questions.length === 0 && <div className="q-card"><p>No hay preguntas definidas para esta dimensión.</p></div>}
              {questions.map((question) => {
                const state = openQuestion?.id === question.id ? openQuestion : null;
                return (
                  <article className="q-card" key={question.id}>
                    <div className="q-head">
                      <span className={`type-badge ${question.type === "multidimension" ? "multidim" : "unidim"}`}>{question.id}</span>
                    </div>
                    <h3 className="q-title">{question.title}</h3>
                    {question.description && <p className="q-desc">{question.description}</p>}

                    {state && (
                      <div className={`q-answer ${state.error ? "error" : ""}`}>
                        {state.loading ? (
                          <div className="q-answer-loading">
                            <span className="spinner" aria-hidden="true" />
                            Generando interpretación con IA…
                          </div>
                        ) : (
                          <>
                            {state.model && <span className="q-answer-model">Modelo: {state.model}</span>}
                            <div className="q-answer-text"><AnswerText text={state.text} /></div>
                          </>
                        )}
                      </div>
                    )}

                    <div className="q-actions">
                      <button
                        className="btn ai-btn"
                        type="button"
                        disabled={state?.loading}
                        onClick={() => resolveQuestionWithOllama(question)}
                      >
                        {state?.loading ? (
                          <>
                            <span className="spinner" aria-hidden="true" /> Interpretando…
                          </>
                        ) : (
                          <>✨ Interpretar con IA</>
                        )}
                      </button>
                    </div>
                  </article>
                );
              })}
            </div>
          </section>
        )}

        {activeTab === "cuadro-mando" && (
          <section>
            <div className="hero dashboard-hero">
              <div>
                <h1>Cuadro de mando</h1>
                <p>Seguimiento integrado de movilidad, afectaciones urbanas, ITS y ocupación permanente para {user?.municipio_id}.</p>
              </div>
              <button className="btn alt" type="button" onClick={loadDashboard}>Actualizar datos</button>
            </div>

            <div className="kpi-grid">
              <KpiCard label="Tráfico en alto/crítico" value={formatNumber(dashboard.kpis?.trafico_pct_alto_critico, "%")} detail="Tiempo medido en niveles alto o crítico" accent="blue" />
              <KpiCard label="Parking ocupado" value={formatNumber(dashboard.kpis?.ocupacion_parking_media, "%")} detail={`${formatNumber(dashboard.kpis?.plazas_reservadas)} plazas reservadas`} accent="green" />
              <KpiCard label="Afectaciones" value={formatNumber(dashboard.kpis?.total_afectaciones)} detail="Registros urbanos" accent="amber" />
              <KpiCard label="Espacio ocupado" value={formatNumber(dashboard.kpis?.superficie_ocupada_m2, " m2")} detail={`${formatNumber(dashboard.kpis?.total_ocupaciones)} ocupaciones`} accent="red" />
              <KpiCard label="Cobertura PMR (ITS)" value={formatNumber(dashboard.kpis?.its_pct_accesibilidad_pmr, "%")} detail="Dispositivos ITS con accesibilidad PMR" accent="green" />
            </div>

            <div className="dashboard-tabs" role="tablist" aria-label="Vistas del cuadro de mando">
              {[["global", "Global"], ["movilidad", "Movilidad"], ["congestion", "Congestión"], ["cruces", "Cruces"], ["afectaciones", "Afectaciones"], ["its", "Control ITS"], ["ocupacion", "Ocupación permanente"]].map(([key, label]) => (
                <button key={key} className={dashboardView === key ? "selected" : ""} type="button" onClick={() => setDashboardView(key)}>{label}</button>
              ))}
            </div>

            {dashboardView === "global" && (
              <div className="dashboard-layout wide-left">
                <div className="card priority-panel">
                  <div className="panel-heading"><span className="eyebrow">PRIORIZACIÓN OPERATIVA</span><h2>Zonas que requieren seguimiento</h2></div>
                  {(dashboard.priority_zones || []).length === 0 ? <p className="chart-empty">Aún no hay datos cruzados suficientes.</p> : dashboard.priority_zones.map((zone) => (
                    <div className="priority-row" key={zone.via}>
                      <div><div className="priority-title"><strong>{zone.via}</strong><span className={`risk-badge risk-${zone.nivel}`}>{zone.nivel}</span></div><p>{zone.reasons?.join(" · ")}</p></div>
                      <strong className="priority-score">{formatNumber(zone.score, "%")}</strong>
                    </div>
                  ))}
                </div>
                <div className="dashboard-charts single-column">
                  <BarChart title="Vías más congestionadas (% tiempo alto/crítico)" items={dashboard.mobility?.trafico_por_via || []} labelKey="via" suffix="%" />
                  <BarChart title="Superficie ocupada por vía" items={dashboard.occupancy?.superficie_por_via || []} labelKey="via" suffix=" m2" />
                </div>

                <article className="card city-map-panel">
                  <div className="panel-heading">
                    <span className="eyebrow">GEOLOCALIZACIÓN REAL</span>
                    <h2>Afectaciones más críticas en el mapa</h2>
                  </div>
                  <CityMap points={(dashboard.priority_zones || []).filter((zone) => zone.lat != null && zone.lon != null)} />
                  <div className="map-legend">
                    <span><i style={{ background: RISK_COLORS.critico }} />Crítico</span>
                    <span><i style={{ background: RISK_COLORS.alto }} />Alto</span>
                    <span><i style={{ background: RISK_COLORS.medio }} />Medio</span>
                    <span><i style={{ background: RISK_COLORS.bajo }} />Bajo</span>
                  </div>
                </article>
              </div>
            )}

            {dashboardView === "movilidad" && <div className="dashboard-charts"><BarChart title="Vías más congestionadas (% tiempo alto/crítico)" items={dashboard.mobility?.trafico_por_via || []} labelKey="via" suffix="%" /><BarChart title="Parking más ocupado" items={dashboard.mobility?.parking_ocupacion || []} labelKey="parking" suffix="%" /><BarChart title="Plazas reservadas por tipo" items={dashboard.mobility?.plazas_por_tipo || []} labelKey="tipo_plaza" /></div>}

            {dashboardView === "congestion" && (
              <div className="dashboard-layout wide-left">
                <div className="card priority-panel">
                  <div className="panel-heading">
                    <span className="eyebrow">CAPA 5 · REGLAS SOBRE HISTÓRICO REAL</span>
                    <h2>Vías a intervenir primero</h2>
                  </div>
                  {congestion.loading && <p className="chart-empty">Calculando percentiles de congestión...</p>}
                  {congestion.error && <p className="chart-empty">{congestion.error}</p>}
                  {!congestion.loading && !congestion.error && (congestion.data?.prioridades || []).length === 0 && (
                    <p className="chart-empty">Aún no hay mediciones suficientes de tráfico.</p>
                  )}
                  {(congestion.data?.prioridades || []).map((zone) => (
                    <div className="priority-row" key={zone.direccion}>
                      <div>
                        <div className="priority-title">
                          <strong>{zone.direccion}</strong>
                          <span className={`risk-badge ${riskClassFromPct(zone.pct_tiempo_alto_critico)}`}>
                            {formatNumber(zone.pct_tiempo_alto_critico, "%")} tiempo alto/crítico
                          </span>
                        </div>
                        <p>
                          {zone.mediciones} mediciones · {zone.mediciones_con_obra_activa} con obra activa · umbral crítico {formatNumber(zone.umbral_critico_veh_h)} veh/h
                        </p>
                        <p>{(zone.recomendaciones || []).join(" · ")}</p>
                      </div>
                    </div>
                  ))}
                  {congestion.data?.metricas && (
                    <p className="chart-empty">
                      {formatNumber(congestion.data.metricas.count_alto_critico)} mediciones en nivel alto/crítico de {formatNumber(congestion.data.total_registros)} totales ({formatNumber(congestion.data.metricas.pct_alto_critico, "%")}).
                    </p>
                  )}
                </div>

                <div className="dashboard-charts single-column">
                  <article className="chart-card">
                    <h2>Predecir congestión por vía</h2>
                    <form className="prediccion-form" onSubmit={submitPrediccion}>
                      <label>
                        Vía
                        <input
                          list="direcciones-trafico"
                          value={prediccionForm.direccion}
                          onChange={(event) => setPrediccionForm((current) => ({ ...current, direccion: event.target.value }))}
                          placeholder="Ej. Avenida de Andalucía"
                          required
                        />
                        <datalist id="direcciones-trafico">
                          {(dashboard.mobility?.trafico_por_via || []).map((item) => (
                            <option key={item.via} value={item.via} />
                          ))}
                        </datalist>
                      </label>
                      <label>
                        Franja horaria (opcional)
                        <select
                          value={prediccionForm.franja_horaria}
                          onChange={(event) => setPrediccionForm((current) => ({ ...current, franja_horaria: event.target.value }))}
                        >
                          <option value="">Todas</option>
                          {FRANJAS_HORARIAS.map((franja) => <option key={franja} value={franja}>{franja}</option>)}
                        </select>
                      </label>
                      <label>
                        Día de la semana (opcional)
                        <select
                          value={prediccionForm.dia_semana}
                          onChange={(event) => setPrediccionForm((current) => ({ ...current, dia_semana: event.target.value }))}
                        >
                          <option value="">Todos</option>
                          {DIAS_SEMANA.map((dia) => <option key={dia} value={dia}>{dia}</option>)}
                        </select>
                      </label>
                      <label>
                        Tipo de vehículo (solo modelo entrenado)
                        <select
                          value={prediccionForm.trafico_vehiculo}
                          onChange={(event) => setPrediccionForm((current) => ({ ...current, trafico_vehiculo: event.target.value }))}
                        >
                          {TIPOS_VEHICULO.map((tipo) => <option key={tipo} value={tipo}>{tipo}</option>)}
                        </select>
                      </label>
                      <button className="btn" type="submit" disabled={prediccion.loading}>
                        {prediccion.loading ? "Calculando..." : "Predecir"}
                      </button>
                    </form>

                    {prediccion.error && <p className="chart-empty">{prediccion.error}</p>}
                    {prediccion.data && (
                      <div className="prediccion-result">
                        <div className="priority-title">
                          <strong>{prediccion.data.direccion}</strong>
                          <span className={`risk-badge ${riskClassFromNivel(prediccion.data.nivel_congestion)}`}>{prediccion.data.nivel_congestion}</span>
                        </div>
                        <p>
                          Flujo esperado: {formatNumber(prediccion.data.flujo_esperado_veh_h)} veh/h (umbral crítico {formatNumber(prediccion.data.umbral_critico_veh_h)} veh/h)
                        </p>
                        <p>
                          Confiabilidad {prediccion.data.confiabilidad} ({formatNumber(prediccion.data.muestra_utilizada)} mediciones usadas)
                          {prediccion.data.obra_activa ? " · obra activa en esta vía" : ""}
                        </p>
                        <p><strong>Recomendación:</strong> {prediccion.data.recomendacion}</p>
                      </div>
                    )}

                    <h3 className="prediccion-ml-heading">Según el modelo entrenado (Random Forest)</h3>
                    {prediccionMl.error && <p className="chart-empty">{prediccionMl.error}</p>}
                    {prediccionMl.data && (
                      <div className="prediccion-result">
                        <div className="priority-title">
                          <strong>{prediccionMl.data.direccion}</strong>
                          <span className={`risk-badge ${riskClassFromNivel(prediccionMl.data.nivel_congestion_ml)}`}>{prediccionMl.data.nivel_congestion_ml}</span>
                        </div>
                        <p>Confianza del modelo: {formatNumber(prediccionMl.data.confianza * 100, "%")}</p>
                        <p>
                          {Object.entries(prediccionMl.data.distribucion || {}).map(([nivel, prob]) => `${nivel}: ${formatNumber(prob * 100, "%")}`).join(" · ")}
                        </p>
                      </div>
                    )}
                  </article>

                  <article className="chart-card">
                    <h2>Modelo predictivo (Random Forest)</h2>
                    <p className="chart-empty">
                      Clasificador entrenado sobre las {formatNumber(dashboard.modelo_predictivo?.filas_entrenamiento)} mediciones reales más
                      antiguas y evaluado sobre las {formatNumber(dashboard.modelo_predictivo?.filas_test)} más recientes (split temporal:
                      el modelo nunca ve el periodo con el que se mide). Accuracy real: <strong>{formatNumber((dashboard.modelo_predictivo?.accuracy || 0) * 100, "%")}</strong>.
                    </p>
                    <BarChart
                      title="Importancia de cada variable"
                      items={(dashboard.modelo_predictivo?.importancia_features || []).map((f) => ({ feature: f.feature, count: Math.round(f.importancia * 100) }))}
                      labelKey="feature"
                      suffix="%"
                    />
                  </article>

                  <article className="chart-card">
                    <h2>Efecto mariposa: parking y tráfico cercano (EQ1)</h2>
                    {(dashboard.cruces?.eq1_parking_trafico || []).length === 0 ? (
                      <p className="chart-empty">Sin parkings con un punto de tráfico real a menos de 400 m.</p>
                    ) : (
                      dashboard.cruces.eq1_parking_trafico.map((item) => (
                        <div className="priority-row" key={item.direccion}>
                          <div>
                            <div className="priority-title">
                              <strong>{item.parking}</strong>
                              <span className="risk-badge risk-alto">{formatNumber(item.pct_saturacion, "%")} saturado</span>
                            </div>
                            <p>Vía de tráfico más cercana: {item.via_trafico_cercana} ({formatNumber(item.distancia_m)} m) · {formatNumber(item.pct_congestion_via, "%")} tiempo en alto/crítico</p>
                          </div>
                        </div>
                      ))
                    )}
                  </article>

                  <article className="chart-card">
                    <h2>Impacto de obras en el tráfico (EQ6)</h2>
                    {(dashboard.obras_impacto_trafico || []).length === 0 ? (
                      <p className="chart-empty">
                        Sin obras con mediciones de tráfico solapadas todavía: los datos reales de
                        tráfico solo cubren 3 fechas (feb-2026) mientras que las afectaciones se
                        reparten por todo el año, así que casi nunca coinciden en el tiempo.
                      </p>
                    ) : (
                      dashboard.obras_impacto_trafico.map((obra) => (
                        <div className="priority-row" key={`${obra.nombre}-${obra.direccion}`}>
                          <div>
                            <div className="priority-title">
                              <strong>{obra.nombre}</strong>
                              <span className={`risk-badge ${riskClassFromNivel(obra.impacto_estimado)}`}>{obra.impacto_estimado}</span>
                            </div>
                            <p>{obra.direccion} · {obra.tipo_intervencion}</p>
                            <p>Flujo antes: {formatNumber(obra.flujo_antes)} veh/h · durante: {formatNumber(obra.flujo_durante)} veh/h</p>
                          </div>
                          <strong className="priority-score">{formatNumber(obra.variacion_trafico_pct, "%")}</strong>
                        </div>
                      ))
                    )}
                  </article>
                </div>
              </div>
            )}

            {dashboardView === "cruces" && (
              <div className="dashboard-layout">
                <article className="chart-card">
                  <h2>Puntos ciegos: obras sin cobertura ITS (EQ2)</h2>
                  <p className="chart-empty">Vías con afectaciones activas y cuántos dispositivos ITS (cámaras, PMV) hay en esa misma vía.</p>
                  {(dashboard.cruces?.eq2_afectaciones_its || []).length === 0 ? (
                    <p className="chart-empty">Sin vías con afectaciones registradas todavía.</p>
                  ) : (
                    dashboard.cruces.eq2_afectaciones_its.map((via) => (
                      <div className="priority-row" key={via.direccion}>
                        <div>
                          <div className="priority-title">
                            <strong>{via.direccion}</strong>
                            {via.punto_ciego && <span className="risk-badge risk-critico">punto ciego</span>}
                          </div>
                          <p>{via.afectaciones} afectaciones ({via.cortes_trafico} cortes de tráfico) · {via.dispositivos_its} dispositivos ITS</p>
                        </div>
                      </div>
                    ))
                  )}
                </article>

                <article className="chart-card">
                  <h2>Ocupación y afectaciones simultáneas (EQ3)</h2>
                  <p className="chart-empty">Vías donde coinciden terrazas activas y obras/cortes -- presión sobre la red peatonal.</p>
                  {(dashboard.cruces?.eq3_ocupacion_afectaciones || []).length === 0 ? (
                    <p className="chart-empty">Sin vías con ambos tipos de registro todavía.</p>
                  ) : (
                    dashboard.cruces.eq3_ocupacion_afectaciones.map((via) => (
                      <div className="priority-row" key={via.direccion}>
                        <div>
                          <div className="priority-title">
                            <strong>{via.direccion}</strong>
                            {via.impacto_pmr && <span className="risk-badge risk-critico">impacto PMR</span>}
                          </div>
                          <p>{via.terrazas} terrazas ({formatNumber(via.superficie_m2, " m²")}) · {via.afectaciones} afectaciones activas</p>
                        </div>
                      </div>
                    ))
                  )}
                </article>

                <article className="chart-card">
                  <h2>Terrazas vs. carga y descarga (EQ4)</h2>
                  <p className="chart-empty">Vías con más terrazas y menos plazas de carga/descarga disponibles -- presión logística.</p>
                  {(dashboard.cruces?.eq4_ocupacion_carga_trafico || []).length === 0 ? (
                    <p className="chart-empty">Sin vías con ambos tipos de registro todavía.</p>
                  ) : (
                    dashboard.cruces.eq4_ocupacion_carga_trafico.map((via) => (
                      <div className="priority-row" key={via.direccion}>
                        <div>
                          <div className="priority-title"><strong>{via.direccion}</strong></div>
                          <p>
                            {via.terrazas} terrazas ({formatNumber(via.superficie_m2, " m²")}) · {via.plazas_carga_descarga} plazas de carga/descarga
                            {via.pct_congestion != null ? ` · ${formatNumber(via.pct_congestion, "%")} tiempo en alto/crítico` : ""}
                          </p>
                        </div>
                      </div>
                    ))
                  )}
                </article>
              </div>
            )}

            {dashboardView === "afectaciones" &&<div className="dashboard-charts"><BarChart title="Vías con más afectaciones" items={dashboard.summary?.top_vias || []} labelKey="via" /><BarChart title="Afectaciones por tipo" items={dashboard.summary?.por_tipo_afectacion || []} labelKey="tipo_afectacion" /><BarChart title="Actividad por hora" items={dashboard.summary?.por_hora || []} labelKey="hora" /></div>}
            {dashboardView === "its" && <div className="dashboard-charts"><BarChart title="ITS por categoría" items={dashboard.its?.por_categoria || []} labelKey="categoria" /></div>}
            {dashboardView === "ocupacion" && <div className="dashboard-charts"><BarChart title="Ocupación por tipo" items={dashboard.occupancy?.por_tipo || []} labelKey="tipo_ocupacion" /><BarChart title="Superficie por vía" items={dashboard.occupancy?.superficie_por_via || []} labelKey="via" suffix=" m2" /></div>}
          </section>
        )}

        {activeTab === "catalogo-datos" && (
          <section>
            <div className="hero">
              <div>
                <h1>Catálogo de datos</h1>
                <p>Entregas reales de todo el espacio de datos: las tuyas propias y las de otros municipios marcadas como compartidas.</p>
              </div>
              <button className="btn alt" type="button" onClick={loadCatalogoEntregas}>Actualizar</button>
            </div>

            {catalogoStatus.message && <p className={`admin-alert ${catalogoStatus.type}`}>{catalogoStatus.message}</p>}

            <div className="card">
              <table className="admin-table">
                <thead>
                  <tr><th>Municipio</th><th>Dataset</th><th>Periodo</th><th>Filas</th><th>Visibilidad</th><th></th></tr>
                </thead>
                <tbody>
                  {catalogoEntregas.length === 0 && !catalogoStatus.message && (
                    <tr><td colSpan={6}><p className="chart-empty">No hay entregas compartidas disponibles todavía.</p></td></tr>
                  )}
                  {catalogoEntregas.map((entrega) => (
                    <tr key={entrega.id}>
                      <td>{entrega.municipio_id}</td>
                      <td>{entrega.dataset}</td>
                      <td>{entrega.period || "—"} {entrega.anio || ""}</td>
                      <td>{entrega.row_count ?? "—"}</td>
                      <td>{entrega.visibilidad === "privado" ? "Privado (tuyo)" : "Compartido"}</td>
                      <td><button className="btn primary" type="button" onClick={() => handleDescargarEntrega(entrega)}>Descargar CSV</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="chart-empty" style={{ marginTop: "0.8rem" }}>
                El CSV descargado es el dato ya validado y tipado (Capa 4), no el fichero original subido. Su uso está sujeto a las condiciones de uso vigentes del espacio de datos.
              </p>
            </div>
          </section>
        )}

        {activeTab === "administracion" && canSeeAdminPanel && (
          <section>
            <div className="hero">
              <div>
                <h1>Administración del espacio de datos</h1>
                <p>Gestión de usuarios, supervisión de ingestas, catálogo de contratos y auditoría de condiciones de uso.</p>
              </div>
              <button className="btn alt" type="button" onClick={loadAdminPanel}>Actualizar</button>
            </div>

            {adminStatus.message && <p className={`admin-alert ${adminStatus.type}`}>{adminStatus.message}</p>}

            <div className="dashboard-tabs" role="tablist" aria-label="Secciones de administración">
              {[["usuarios", "Usuarios"], ["ingestas", "Ingestas"], ["catalogo", "Catálogo"], ["contratos", "Contratos"], ["politicas", "Políticas"]]
                .filter(([key]) => isAdmin || key === "ingestas" || key === "politicas")
                .map(([key, label]) => (
                  <button key={key} className={adminSubTab === key ? "selected" : ""} type="button" onClick={() => setAdminSubTab(key)}>{label}</button>
                ))}
            </div>

            {adminSubTab === "usuarios" && isAdmin && (
              <div className="card">
                <div className="panel-heading"><h2>Usuarios</h2></div>

                <form className="admin-form" onSubmit={handleCreateUsuario}>
                  <label>Nombre<input value={newUserForm.name} onChange={(event) => setNewUserForm((current) => ({ ...current, name: event.target.value }))} required /></label>
                  <label>Email<input type="email" value={newUserForm.email} onChange={(event) => setNewUserForm((current) => ({ ...current, email: event.target.value }))} required /></label>
                  <label>Contraseña<input type="password" value={newUserForm.password} onChange={(event) => setNewUserForm((current) => ({ ...current, password: event.target.value }))} required /></label>
                  <label>Municipio<input value={newUserForm.municipio_id} onChange={(event) => setNewUserForm((current) => ({ ...current, municipio_id: event.target.value }))} placeholder="MALAGA" required /></label>
                  <label>
                    Rol
                    <select value={newUserForm.role} onChange={(event) => setNewUserForm((current) => ({ ...current, role: event.target.value }))}>
                      {["admin_estatal", "editor_municipio", "lector_municipio", "consumidor", "auditor"].map((role) => (
                        <option key={role} value={role}>{role}</option>
                      ))}
                    </select>
                  </label>
                  <button className="btn primary" type="submit">Crear usuario</button>
                </form>

                <table className="admin-table">
                  <thead>
                    <tr><th>Nombre</th><th>Email</th><th>Municipio</th><th>Rol</th><th>Estado</th><th></th></tr>
                  </thead>
                  <tbody>
                    {adminUsuarios.map((usuario) => (
                      <tr key={usuario.id}>
                        <td>{usuario.name}</td>
                        <td>{usuario.email}</td>
                        <td>{usuario.municipio_id}</td>
                        <td>
                          <select value={usuario.role} onChange={(event) => handleUpdateUsuario(usuario.id, { role: event.target.value })}>
                            {["admin_estatal", "editor_municipio", "lector_municipio", "consumidor", "auditor"].map((role) => (
                              <option key={role} value={role}>{role}</option>
                            ))}
                          </select>
                        </td>
                        <td>{usuario.activo ? "Activo" : "Desactivado"}</td>
                        <td className="admin-actions-cell">
                          <button className="btn alt" type="button" onClick={() => handleUpdateUsuario(usuario.id, { activo: !usuario.activo })}>
                            {usuario.activo ? "Desactivar" : "Activar"}
                          </button>
                          <button className="btn danger" type="button" onClick={() => handleDeleteUsuario(usuario.id, usuario.email, usuario.role)}>Borrar</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {adminSubTab === "ingestas" && (
              <div className="card">
                <div className="panel-heading"><h2>Supervisión de ingestas</h2></div>
                <table className="admin-table">
                  <thead>
                    <tr><th>Recibida</th><th>Municipio</th><th>Dataset</th><th>Estado</th><th>Validación</th><th></th></tr>
                  </thead>
                  <tbody>
                    {adminIngestas.map((entrega) => (
                      <tr key={entrega.id}>
                        <td>{entrega.received_at ? new Date(entrega.received_at).toLocaleString("es-ES") : "—"}</td>
                        <td>{entrega.municipio_id || entrega.entity}</td>
                        <td>{entrega.dataset}</td>
                        <td>{entrega.status}{entrega.indicador_conflicto ? " · conflicto" : ""}</td>
                        <td>{entrega.resultado_validacion || "—"}{entrega.numero_registros_con_incidencia ? ` (${entrega.numero_registros_con_incidencia} incidencias)` : ""}</td>
                        <td><button className="btn alt" type="button" onClick={() => handleVerDetalleIngesta(entrega.id)}>Ver detalle</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {adminSubTab === "catalogo" && isAdmin && (
              <div className="card">
                <div className="panel-heading">
                  <h2>Catálogo de contratos</h2>
                  <button className="btn" type="button" onClick={handleResincronizarCatalogo}>
                    Resincronizar catálogo
                  </button>
                </div>
                <table className="admin-table">
                  <thead>
                    <tr><th>Dataset</th><th>Dimensión</th><th>Estado</th><th>Actualizado</th><th></th></tr>
                  </thead>
                  <tbody>
                    {adminCatalogo.map((item) => (
                      <tr key={item.id}>
                        <td>{item.display_name || item.id}</td>
                        <td>{item.dimension}</td>
                        <td>{item.status === "active" ? "Activo" : "Inactivo"}</td>
                        <td>{item.updated_at ? new Date(item.updated_at).toLocaleString("es-ES") : "—"}</td>
                        <td>
                          <button className="btn alt" type="button" onClick={() => handleToggleCatalogo(item.id, item.status)}>
                            {item.status === "active" ? "Desactivar" : "Activar"}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                <div className="panel-heading mt-16"><h2>Historial de sincronización (gobernanza común)</h2></div>
                <table className="admin-table">
                  <thead>
                    <tr><th>Fecha</th><th>Admin</th><th>Estado</th><th>Datasets</th></tr>
                  </thead>
                  <tbody>
                    {adminCatalogoHistorial.map((item) => (
                      <tr key={item.id}>
                        <td>{item.sincronizado_en ? new Date(item.sincronizado_en).toLocaleString("es-ES") : "—"}</td>
                        <td>{item.sincronizado_por ?? "sistema"}</td>
                        <td>{item.status}</td>
                        <td>{(item.datasets || []).length} datasets</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {adminSubTab === "contratos" && isAdmin && (
              <div className="card">
                <div className="panel-heading">
                  <h2>Crear contrato de datos</h2>
                </div>
                <p className="chart-empty">
                  Da de alta un dataset nuevo sin tocar código: define sus columnas y a partir de
                  ahora aparecerá en el selector de subida, con la misma validación, tipado y
                  cuadro de mando genéricos que cualquier dimensión existente. Las columnas
                  calculadas (fórmulas) no están disponibles aquí — si una dimensión nueva las
                  necesita, sigue siendo trabajo de desarrollo.
                </p>

                <form className="admin-form" onSubmit={handleCrearContrato}>
                  <label>
                    Identificador del dataset
                    <input
                      value={nuevoContratoForm.dataset_id}
                      onChange={(event) => setNuevoContratoForm((current) => ({ ...current, dataset_id: event.target.value }))}
                      placeholder="arbolado_urbano"
                      required
                    />
                  </label>
                  <label>
                    Dimensión
                    <input
                      value={nuevoContratoForm.dimension}
                      onChange={(event) => setNuevoContratoForm((current) => ({ ...current, dimension: event.target.value }))}
                      placeholder="arbolado_urbano"
                      required
                    />
                  </label>
                  <label>
                    Nombre visible
                    <input
                      value={nuevoContratoForm.display_name}
                      onChange={(event) => setNuevoContratoForm((current) => ({ ...current, display_name: event.target.value }))}
                      placeholder="Arbolado urbano"
                      required
                    />
                  </label>

                  <div className="contrato-columnas">
                    <div className="panel-heading mt-16"><h3>Columnas</h3></div>
                    {nuevoContratoForm.columnas.map((columna, index) => (
                      <div className="contrato-columna-row" key={index}>
                        <input
                          className="contrato-columna-nombre"
                          value={columna.nombre}
                          onChange={(event) => handleContratoColumnaChange(index, { nombre: event.target.value })}
                          placeholder="nombre_columna"
                        />
                        <select
                          value={columna.tipo}
                          onChange={(event) => handleContratoColumnaChange(index, { tipo: event.target.value })}
                        >
                          <option value="texto">Texto</option>
                          <option value="numero_entero">Número entero (≥0)</option>
                          <option value="numero_decimal">Número decimal</option>
                          <option value="fecha">Fecha</option>
                          <option value="hora">Hora</option>
                          <option value="coordenada">Coordenada (latitud/longitud)</option>
                          <option value="booleano">Booleano (sí/no)</option>
                        </select>
                        <label className="contrato-columna-obligatoria">
                          <input
                            type="checkbox"
                            checked={columna.obligatoria}
                            onChange={(event) => handleContratoColumnaChange(index, { obligatoria: event.target.checked })}
                          />
                          Obligatoria
                        </label>
                        <input
                          className="contrato-columna-valores"
                          value={columna.valores_permitidos}
                          onChange={(event) => handleContratoColumnaChange(index, { valores_permitidos: event.target.value })}
                          placeholder="valores permitidos, separados por coma (opcional)"
                        />
                        <button
                          className="btn danger"
                          type="button"
                          onClick={() => handleRemoveContratoColumna(index)}
                          disabled={nuevoContratoForm.columnas.length === 1}
                        >
                          Quitar
                        </button>
                      </div>
                    ))}
                    <button className="btn alt" type="button" onClick={handleAddContratoColumna}>+ Añadir columna</button>
                  </div>

                  <button className="btn primary" type="submit">Crear contrato</button>
                </form>

                <div className="panel-heading mt-16"><h2>Contratos personalizados</h2></div>
                {adminContratos.length === 0 ? (
                  <p className="chart-empty">Todavía no se ha creado ningún contrato desde este panel.</p>
                ) : (
                  <table className="admin-table">
                    <thead>
                      <tr><th>Dataset</th><th>Dimensión</th><th>Nombre visible</th><th>Creado</th></tr>
                    </thead>
                    <tbody>
                      {adminContratos.map((item) => (
                        <tr key={item.dataset_id}>
                          <td>{item.dataset_id}</td>
                          <td>{item.dimension}</td>
                          <td>{item.display_name}</td>
                          <td>{item.creado_en ? new Date(item.creado_en).toLocaleString("es-ES") : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}

            {adminSubTab === "politicas" && (
              <div className="card">
                <div className="panel-heading"><h2>Condiciones de uso</h2></div>
                {isAdmin && (
                  <form className="admin-form" onSubmit={handlePublishPolicy}>
                    <label>Versión<input value={adminPolicyForm.version} onChange={(event) => setAdminPolicyForm((current) => ({ ...current, version: event.target.value }))} placeholder="v2" required /></label>
                    <label>Título<input value={adminPolicyForm.titulo} onChange={(event) => setAdminPolicyForm((current) => ({ ...current, titulo: event.target.value }))} required /></label>
                    <label>Contenido<textarea rows={4} value={adminPolicyForm.contenido} onChange={(event) => setAdminPolicyForm((current) => ({ ...current, contenido: event.target.value }))} required /></label>
                    <button className="btn primary" type="submit">Publicar nueva versión</button>
                  </form>
                )}
                <table className="admin-table">
                  <thead>
                    <tr><th>Usuario</th><th>Municipio</th><th>Rol</th><th>Versión vigente</th><th>Aceptada</th><th>Aceptado el</th></tr>
                  </thead>
                  <tbody>
                    {adminAceptaciones.map((item) => (
                      <tr key={item.user_id}>
                        <td>{item.name}</td>
                        <td>{item.municipio_id}</td>
                        <td>{item.role}</td>
                        <td>{item.version_vigente}</td>
                        <td>{item.aceptada ? "Sí" : "No"}</td>
                        <td>{item.aceptado_en ? new Date(item.aceptado_en).toLocaleString("es-ES") : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        )}
      </main>

      {policyModal.open && (
        <div className="modal-overlay">
          <div className="card modal-card">
            <div className="panel-heading"><h2>{policyModal.titulo}</h2></div>
            <p className="chart-empty">Versión {policyModal.version}</p>
            <p>{policyModal.contenido}</p>
            <div className="modal-actions">
              <button className="btn alt" type="button" onClick={handleCancelPolicy}>Cancelar</button>
              <button className="btn primary" type="button" onClick={handleAcceptPolicy}>Acepto y continúo</button>
            </div>
          </div>
        </div>
      )}

      {ingestaDetalle && (
        <div className="modal-overlay">
          <div className="card modal-card modal-wide">
            <div className="panel-heading">
              <h2>Entrega #{ingestaDetalle.entrega.id} · {ingestaDetalle.entrega.dataset}</h2>
              <div className="modal-heading-actions">
                <button
                  className="btn danger"
                  type="button"
                  onClick={() => handleBorrarIngesta(
                    ingestaDetalle.entrega.id,
                    ingestaDetalle.entrega.dataset,
                    ingestaDetalle.entrega.municipio_id || ingestaDetalle.entrega.entity,
                  )}
                >
                  Borrar entrega
                </button>
                <button className="btn alt" type="button" onClick={() => setIngestaDetalle(null)}>Cerrar</button>
              </div>
            </div>
            <dl className="detail-grid">
              <div><dt>Municipio</dt><dd>{ingestaDetalle.entrega.municipio_id || ingestaDetalle.entrega.entity}</dd></div>
              <div><dt>Periodo</dt><dd>{ingestaDetalle.entrega.period || "—"} {ingestaDetalle.entrega.anio || ""}</dd></div>
              <div><dt>Visibilidad</dt><dd>{ingestaDetalle.entrega.visibilidad === "privado" ? "Privado" : "Compartido"}</dd></div>
              <div><dt>Estado</dt><dd>{ingestaDetalle.entrega.status}</dd></div>
              <div><dt>Canal de entrada</dt><dd>{ingestaDetalle.entrega.entry_channel}</dd></div>
              <div><dt>Remitente</dt><dd>{ingestaDetalle.entrega.sender}</dd></div>
              <div><dt>Fichero</dt><dd>{ingestaDetalle.entrega.filename}</dd></div>
              <div><dt>Conflicto</dt><dd>{ingestaDetalle.entrega.indicador_conflicto ? `Sí (${ingestaDetalle.entrega.decision_sobre_conflicto || "sin decisión"})` : "No"}</dd></div>
            </dl>

            <h3>Eventos de recepción</h3>
            {ingestaDetalle.eventos.length === 0 ? <p className="chart-empty">Sin eventos registrados.</p> : (
              <table className="admin-table">
                <thead><tr><th>Fecha</th><th>Evento</th><th>Actor</th><th>Observación</th></tr></thead>
                <tbody>
                  {ingestaDetalle.eventos.map((evento, index) => (
                    <tr key={index}>
                      <td>{evento.created_at ? new Date(evento.created_at).toLocaleString("es-ES") : "—"}</td>
                      <td>{evento.event_type}</td>
                      <td>{evento.actor}</td>
                      <td>{evento.observation}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <h3>Incidencias de validación {ingestaDetalle.incidencias.length > 0 ? `(${ingestaDetalle.incidencias.length})` : ""}</h3>
            {ingestaDetalle.incidencias.length === 0 ? <p className="chart-empty">Sin incidencias registradas.</p> : (
              <table className="admin-table">
                <thead><tr><th>Fila</th><th>Campo</th><th>Regla</th><th>Severidad</th><th>Descripción</th></tr></thead>
                <tbody>
                  {ingestaDetalle.incidencias.map((incidencia, index) => (
                    <tr key={index}>
                      <td>{incidencia.row_number_origen ?? "—"}</td>
                      <td>{incidencia.campo_afectado || "—"}</td>
                      <td>{incidencia.codigo_regla}</td>
                      <td>{incidencia.severidad}</td>
                      <td>{incidencia.descripcion_incidencia}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}
    </>
  );
}
