-- FIXTURE DE SOLO DESARROLLO LOCAL. NO ES PARTE DEL ESQUEMA DE PRODUCCIÓN.
-- NUNCA se ejecuta contra Supabase real, donde auth.users ya existe y es
-- gestionado por Supabase Auth. Este archivo NO es una migración de Alembic
-- y no debe colocarse dentro de backend/migrations/versions/.
--
-- Propósito único: satisfacer la FK profiles.id -> auth.users(id) para poder
-- validar 001_initial_schema.sql contra un PostgreSQL local sin Supabase.
-- No agregues columnas, triggers, ni lógica adicional — cualquier ampliación
-- de este archivo hacia "simular" Supabase Auth requiere aprobación explícita
-- por separado, no se hace de forma incremental sin pedirla.

CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE IF NOT EXISTS auth.users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);
