/**
 * Form Validation Utility
 * Comprehensive client-side validation for user registration and other forms
 */

// Validation Patterns
const ValidationPatterns = {
    username: /^[a-zA-Z0-9_]{3,20}$/,  // Letters, numbers, underscore, 3-20 chars
    email: /^[^\s@]+@[^\s@]+\.[^\s@]+$/,
    password: /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&.#])[A-Za-z\d@$!%*?&.#]{8,}$/,
    phone: /^[0-9+()\- ]{7,20}$/,
    letters: /^[a-zA-Z\s]+$/,
    numbers: /^[0-9]+$/,
};

// ============ INPUT FORMATTING FUNCTIONS ============

/**
 * Allow only numbers in input
 * @param {HTMLInputElement} input
 */
function onlyNumbers(input) {
    input.value = input.value.replace(/[^0-9]/g, '');
}

/**
 * Allow only letters and spaces in input
 * @param {HTMLInputElement} input
 */
function onlyLetters(input) {
    input.value = input.value.replace(/[^a-zA-Z\s]/g, '');
}

/**
 * Allow only alphanumeric characters
 * @param {HTMLInputElement} input
 */
function onlyAlphanumeric(input) {
    input.value = input.value.replace(/[^a-zA-Z0-9]/g, '');
}

/**
 * Allow only alphanumeric and underscore
 * @param {HTMLInputElement} input
 */
function onlyUsernameChars(input) {
    input.value = input.value.replace(/[^a-zA-Z0-9_]/g, '');
}

// ============ VALIDATION FUNCTIONS ============

/**
 * Validate username (3-20 chars, letters/numbers/underscore only)
 * @param {HTMLInputElement} input
 * @returns {boolean}
 */
function validateUsername(input) {
    const value = input.value.trim();
    const feedback = document.getElementById(input.id + 'Feedback');
    const message = document.getElementById(input.id + 'Message');
    let isValid = false;

    if (!value) {
        showFeedback(feedback, message, false, 'Username is required');
    } else if (value.length < 3) {
        showFeedback(feedback, message, false, 'Username must be at least 3 characters');
    } else if (value.length > 20) {
        showFeedback(feedback, message, false, 'Username must not exceed 20 characters');
    } else if (!/^[a-zA-Z0-9_]+$/.test(value)) {
        showFeedback(feedback, message, false, 'Username can only contain letters, numbers, and underscores');
    } else {
        showFeedback(feedback, message, true, 'Username is valid');
        isValid = true;
    }

    if (feedback) updateSubmitButton();
    return isValid;
}

/**
 * Validate email format
 * @param {HTMLInputElement} input
 * @returns {boolean}
 */
function validateEmail(input) {
    const value = input.value.trim();
    const feedback = document.getElementById(input.id + 'Feedback');
    const message = document.getElementById(input.id + 'Message');
    let isValid = false;

    if (!value) {
        showFeedback(feedback, message, false, 'Email is required');
    } else if (!ValidationPatterns.email.test(value)) {
        showFeedback(feedback, message, false, 'Please enter a valid email address');
    } else {
        showFeedback(feedback, message, true, 'Email is valid');
        isValid = true;
    }

    if (feedback) updateSubmitButton();
    return isValid;
}

/**
 * Validate password strength with requirements checklist
 * @param {HTMLInputElement} input
 * @returns {boolean}
 */
function validatePassword(input) {
    const value = input.value;
    const feedback = document.getElementById(input.id + 'Feedback');
    const message = document.getElementById(input.id + 'Message');
    const checklist = document.getElementById('passwordChecklist');

    if (!value) {
        showFeedback(feedback, message, false, 'Password is required');
        if (checklist) checklist.classList.remove('show');
        return false;
    }

    if (checklist) checklist.classList.add('show');

    // Check each requirement
    const requirements = {
        'check-length': value.length >= 8,
        'check-upper': /[A-Z]/.test(value),
        'check-lower': /[a-z]/.test(value),
        'check-number': /\d/.test(value),
        'check-special': /[@$!%*?&.#]/.test(value),
    };

    // Update checklist display
    Object.entries(requirements).forEach(([id, valid]) => {
        const elem = document.getElementById(id);
        if (elem) {
            if (valid) {
                elem.classList.add('valid');
                const icon = elem.querySelector('i');
                if (icon) icon.className = 'fa-solid fa-check';
            } else {
                elem.classList.remove('valid');
                const icon = elem.querySelector('i');
                if (icon) icon.className = 'fa-solid fa-circle';
            }
        }
    });

    // Check if all requirements are met
    const isValid = Object.values(requirements).every(v => v);

    if (isValid) {
        showFeedback(feedback, message, true, 'Password is strong');
        validatePasswordMatch(document.getElementById('password2'));
    } else {
        showFeedback(feedback, message, false, 'Password does not meet requirements');
    }

    if (feedback) updateSubmitButton();
    return isValid;
}

/**
 * Validate password match/confirmation
 * @param {HTMLInputElement} input
 * @returns {boolean}
 */
function validatePasswordMatch(input) {
    const password = document.getElementById('password');
    const value = input.value;
    const feedback = document.getElementById(input.id + 'Feedback');
    const message = document.getElementById(input.id + 'Message');
    let isValid = false;

    if (!password) return false;

    if (!value) {
        showFeedback(feedback, message, false, 'Please confirm your password');
    } else if (value !== password.value) {
        showFeedback(feedback, message, false, 'Passwords do not match');
    } else {
        showFeedback(feedback, message, true, 'Passwords match');
        isValid = true;
    }

    if (feedback) updateSubmitButton();
    return isValid;
}

/**
 * Validate phone number
 * @param {HTMLInputElement} input
 * @returns {boolean}
 */
function validatePhone(input) {
    const value = input.value.trim();
    const feedback = document.getElementById(input.id + 'Feedback');
    const message = document.getElementById(input.id + 'Message');
    let isValid = false;

    if (!value) {
        showFeedback(feedback, message, false, 'Phone number is required');
    } else if (!/^[0-9+()\- ]{7,20}$/.test(value)) {
        showFeedback(feedback, message, false, 'Please enter a valid phone number');
    } else {
        showFeedback(feedback, message, true, 'Phone number is valid');
        isValid = true;
    }

    if (feedback) updateSubmitButton();
    return isValid;
}

/**
 * Validate name (letters only)
 * @param {HTMLInputElement} input
 * @returns {boolean}
 */
function validateName(input) {
    const value = input.value.trim();
    const feedback = document.getElementById(input.id + 'Feedback');
    const message = document.getElementById(input.id + 'Message');
    let isValid = false;

    if (!value) {
        showFeedback(feedback, message, false, 'Name is required');
    } else if (!/^[a-zA-Z\s]+$/.test(value)) {
        showFeedback(feedback, message, false, 'Name can only contain letters');
    } else {
        showFeedback(feedback, message, true, 'Name is valid');
        isValid = true;
    }

    if (feedback) updateSubmitButton();
    return isValid;
}

// ============ UI HELPER FUNCTIONS ============

/**
 * Show validation feedback (icon and message)
 * @param {HTMLElement} feedbackEl - Icon element
 * @param {HTMLElement} messageEl - Message element
 * @param {boolean} isValid
 * @param {string} text - Message text
 */
function showFeedback(feedbackEl, messageEl, isValid, text) {
    if (!feedbackEl || !messageEl) return;

    if (isValid) {
        feedbackEl.textContent = '✔';
        feedbackEl.className = 'validation-feedback valid';
        messageEl.textContent = text;
        messageEl.className = 'validation-message show success';
    } else {
        feedbackEl.textContent = '❌';
        feedbackEl.className = 'validation-feedback invalid';
        messageEl.textContent = text;
        messageEl.className = 'validation-message show';
    }
}

/**
 * Check if all form fields are valid
 * @returns {boolean}
 */
function areAllFieldsValid() {
    const usernameInput = document.getElementById('username');
    const emailInput = document.getElementById('email');
    const passwordInput = document.getElementById('password');
    const password2Input = document.getElementById('password2');

    let isValid = true;

    if (usernameInput) {
        isValid = validateUsername(usernameInput) && isValid;
    }
    if (emailInput) {
        isValid = validateEmail(emailInput) && isValid;
    }
    if (passwordInput) {
        isValid = validatePassword(passwordInput) && isValid;
    }
    if (password2Input) {
        isValid = validatePasswordMatch(password2Input) && isValid;
    }

    return isValid;
}

/**
 * Update submit button state based on form validation
 */
function updateSubmitButton() {
    const submitBtn = document.getElementById('submitBtn');
    const summary = document.getElementById('validationSummary');

    if (!submitBtn) return;

    if (areAllFieldsValid()) {
        submitBtn.disabled = false;
        if (summary) {
            summary.className = 'validation-summary show';
            summary.textContent = '🟢 All fields are valid. You can now create your account.';
        }
    } else {
        submitBtn.disabled = true;
        if (summary) {
            summary.className = 'validation-summary show error';
            summary.textContent = '⚠️ Please fill in all fields correctly.';
        }
    }
}

// ============ INITIALIZATION ============

/**
 * Initialize form validation on page load
 */
document.addEventListener('DOMContentLoaded', function() {
    updateSubmitButton();

    // Add real-time validation listeners
    const usernameInput = document.getElementById('username');
    const emailInput = document.getElementById('email');
    const passwordInput = document.getElementById('password');
    const password2Input = document.getElementById('password2');

    if (usernameInput) {
        usernameInput.addEventListener('input', function() {
            validateUsername(this);
        });
    }

    if (emailInput) {
        emailInput.addEventListener('input', function() {
            validateEmail(this);
        });
    }

    if (passwordInput) {
        passwordInput.addEventListener('input', function() {
            validatePassword(this);
        });
    }

    if (password2Input) {
        password2Input.addEventListener('input', function() {
            validatePasswordMatch(this);
        });
    }

    // File input validation for customer images (type + size)
    function validateFileInput(input, maxSizeMB = 2) {
        if (!input || !input.files || input.files.length === 0) return true;
        const file = input.files[0];
        const allowedTypes = ['image/png','image/jpeg','image/jpg','image/gif','image/webp','image/svg+xml','image/bmp'];
        const maxBytes = maxSizeMB * 1024 * 1024;

        if (!file.type || (!file.type.startsWith('image/') && !allowedTypes.includes(file.type))) {
            alert('Please upload a valid image file (png, jpg, jpeg, gif, webp, svg, bmp).');
            input.value = '';
            return false;
        }

        if (file.size > maxBytes) {
            alert('Image must be ' + maxSizeMB + ' MB or smaller.');
            input.value = '';
            return false;
        }

        return true;
    }

    // Attach change listeners to any customer image file inputs
    const customerFileInputs = document.querySelectorAll('input[type=file][name="customer_image"], input[type=file]#booking-upload');
    customerFileInputs.forEach(input => {
        input.addEventListener('change', function() {
            validateFileInput(this, 2);
        });
        // Prevent form submission if invalid file
        const form = input.closest('form');
        if (form) {
            form.addEventListener('submit', function(e) {
                if (!validateFileInput(input, 2)) {
                    e.preventDefault();
                    e.stopPropagation();
                    return false;
                }
            });
        }
    });
});
