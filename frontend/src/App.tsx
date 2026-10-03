import { AvailabilityCard } from './components/AvailabilityCard'
import { DeliveryModeCard } from './components/DeliveryModeCard'
import { InstructionPreview } from './components/InstructionPreview'
import { NotificationProvider } from './components/Notifications'
import { Footer, Header, Hero, TodayHeading } from './components/PageChrome'
import { PreferencesForm } from './components/PreferencesForm'
import { useDashboard } from './hooks/useDashboard'

function DashboardScreen() {
  const {
    dashboard,
    connected,
    pending,
    setAvailability,
    setDeliveryMode,
    saveProfile,
  } = useDashboard()
  const context = dashboard?.context
  const disabled = dashboard === null || !connected
  const ready = context?.setup_complete ?? false
  return (
    <div className="app-shell">
      <Header />
      <main id="today">
        <Hero />
        <TodayHeading localDate={context?.local_date} />
        <section className="today-grid" aria-label="Today's delivery settings">
          <AvailabilityCard
            availability={context?.availability}
            explanation={context?.availability_explanation}
            override={context?.today_override}
            disabled={disabled}
            pending={pending.availability}
            onChange={setAvailability}
          />
          <DeliveryModeCard
            ready={ready}
            active={context?.delivery_mode_active ?? false}
            expired={context?.delivery_mode_expired ?? false}
            expiresAt={dashboard?.delivery_mode.expires_at}
            disabled={disabled}
            pending={pending.mode}
            onChange={setDeliveryMode}
          />
        </section>
        <InstructionPreview
          ready={ready}
          pgName={dashboard?.profile.pg_name}
          instruction={context?.instruction}
          restrictions={context?.restrictions}
        />
        <PreferencesForm
          profile={dashboard?.profile}
          ready={ready}
          disabled={disabled}
          pending={pending.profile}
          onSave={saveProfile}
        />
      </main>
      <Footer />
    </div>
  )
}

export default function App() {
  return (
    <NotificationProvider>
      <DashboardScreen />
    </NotificationProvider>
  )
}
