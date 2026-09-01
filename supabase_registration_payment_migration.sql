alter table public.registrations
  add column if not exists course_id text,
  add column if not exists course_name text,
  add column if not exists full_name text,
  add column if not exists experience_level text,
  add column if not exists mode text,
  add column if not exists cashfree_order_id text,
  add column if not exists payment_id text,
  add column if not exists registration_status text,
  add column if not exists paid_at timestamptz,
  add column if not exists updated_at timestamptz;

update public.registrations
set
  full_name = coalesce(full_name, name),
  course_id = coalesce(course_id, 'java-fullstack-claude'),
  course_name = coalesce(course_name, course, 'Claude Code for Java Full-Stack Developers'),
  experience_level = coalesce(experience_level, 'Other'),
  mode = coalesce(mode, 'Online'),
  cashfree_order_id = coalesce(cashfree_order_id, order_id),
  registration_status = coalesce(
    registration_status,
    case
      when payment_status = 'PAID' then 'CONFIRMED'
      when payment_status = 'FAILED' then 'PAYMENT_FAILED'
      else 'PAYMENT_PENDING'
    end
  ),
  updated_at = coalesce(updated_at, created_at, now())
where
  full_name is null
  or course_id is null
  or course_name is null
  or experience_level is null
  or mode is null
  or cashfree_order_id is null
  or registration_status is null
  or updated_at is null;

create unique index if not exists registrations_cashfree_order_id_idx
  on public.registrations (cashfree_order_id)
  where cashfree_order_id is not null;

create index if not exists registrations_email_created_at_idx
  on public.registrations (email, created_at desc);

create or replace function public.set_registrations_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists set_registrations_updated_at on public.registrations;
create trigger set_registrations_updated_at
before update on public.registrations
for each row
execute function public.set_registrations_updated_at();

alter table public.registrations enable row level security;
