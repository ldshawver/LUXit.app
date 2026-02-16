(function () {
  function selectCompany(companyId) {
    fetch(`/companies/switch/${companyId}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      }
    })
      .then((response) => response.json())
      .then((data) => {
        if (data.success) {
          location.reload();
        } else {
          alert('Failed to switch company');
        }
      })
      .catch((error) => {
        console.error('Error:', error);
        alert('An error occurred while switching companies');
      });
  }

  window.selectCompany = selectCompany;
})();
